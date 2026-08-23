#!/usr/bin/env bash
# ============================================================
# NUC HIFI Endpoint 一键配置脚本 v1.0
# 目标机 : D34010WYB @ Debian 13 (Trixie) 最小化 + sshd
# 用法   : root 执行  bash nuc-setup.sh
# 产出   : squeezelite(systemd) + WOL 持久化 + ha-admin 关机通道
#          + shairport-sync/gmrender-resurrect(disable 备用)
#          + 自检报告 /root/nuc-setup-report.txt
# 设计来源: 六方评审(deepseek/Claude/chatgpt/Gemini/kimi/Glm)合并裁决
# ============================================================
set -uo pipefail

LMS_IP="10.10.10.2"
PLAYER_NAME="NUC-HiFi"
HA_ADMIN="ha-admin"
PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAsCnmUHAmvC7RBuShT9cYwT8TEgqZbf9mn2vmfjRwsj zcode@windows'
REPORT=/root/nuc-setup-report.txt
ETHTOOL=$(command -v ethtool || echo /usr/sbin/ethtool)

PASS=0; FAIL=0
ok()   { echo "[OK] $*"  | tee -a "$REPORT"; PASS=$((PASS+1)); }
ng()   { echo "[NG] $*"  | tee -a "$REPORT"; FAIL=$((FAIL+1)); }
info() { echo "[..] $*"  | tee -a "$REPORT"; }

echo "=== NUC HIFI Endpoint Setup $(date '+%F %T') ===" | tee "$REPORT"

# ---------------- Phase 0: 前置检查 ----------------
info "Phase 0: 前置检查"
if [[ $EUID -eq 0 ]]; then ok "root 权限"; else ng "请以 root 运行"; exit 1; fi
. /etc/os-release
if grep -qi "debian" <<< "${ID:-}"; then ok "发行版: ${PRETTY_NAME}"; else ng "非 Debian (${ID:-unknown})"; fi
NET_IF=$(ip -4 route show default | awk '{print $5; exit}')
CUR_IP=$(ip -4 addr show "$NET_IF" 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
MAC=$(cat "/sys/class/net/$NET_IF/address" 2>/dev/null)
if [[ -n "$NET_IF" && -n "$MAC" ]]; then
  ok "网卡 $NET_IF  IP=$CUR_IP  MAC=$MAC"
  echo "$MAC" > /root/nuc-mac.txt
else
  ng "网卡/IP/MAC 探测失败"
fi

# ---------------- Phase 1: 核心包 ----------------
info "Phase 1: 安装核心包(squeezelite/alsa-utils/ethtool/curl/jq/cpufrequtils/openssh-server)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >>"$REPORT" 2>&1 &&
apt-get install -y -qq squeezelite alsa-utils ethtool curl jq cpufrequtils openssh-server >>"$REPORT" 2>&1 &&
  ok "核心包安装完成" || ng "核心包安装失败(查看报告尾部)"

command -v squeezelite >/dev/null && {
  VER=$(squeezelite -t 2>/dev/null | head -1)
  ok "squeezelite 版本: $VER"
} || ng "squeezelite 不可用"

# ---------------- Phase 2: ALSA 输出设备探测(动态,拒绝一刀切) ----------------
info "Phase 2: ALSA 输出设备探测"
SL_LIST=$(squeezelite -l 2>/dev/null)
echo "--- squeezelite -l ---" >> "$REPORT"; echo "$SL_LIST" >> "$REPORT"
CAND=$(echo "$SL_LIST" | grep "hw:CARD=" | sed 's/^[[:space:]]*[0-9]*[[:space:]]*-[[:space:]]*//')
if [[ -z "$CAND" ]]; then
  PICK="default"; ng "无 hw: 设备可用, 回退 default (DAC 未接?)"
else
  # 优先 名称含 USB/DAC 的硬件设备; 否则取第一个 hw:
  PICK=$(echo "$CAND" | grep -im1 -E "usb|dac" | awk '{print $1}')
  [[ -z "$PICK" ]] && PICK=$(echo "$CAND" | head -1 | awk '{print $1}')
  DESC=$(echo "$CAND" | grep -m1 "$PICK" | cut -s -d' ' -f2-)
  ok "选定输出设备: $PICK  ($DESC)"
fi
# 试播校验(squeezelite 尚未启动, 设备空闲)
if command -v speaker-test >/dev/null; then
  speaker-test -c 2 -t wav -D "$PICK" -l 1 >/dev/null 2>&1 &&
    ok "ALSA 试播通过: $PICK" ||
    ng "试播失败($PICK) — 部署后改试 front:CARD=xxx 或 plughw:CARD=xxx"
fi

# ---------------- Phase 3: squeezelite systemd ----------------
info "Phase 3: squeezelite 服务"
id squeezelite &>/dev/null || useradd -r -s /bin/false squeezelite
usermod -aG audio squeezelite 2>/dev/null
cat > /etc/systemd/system/squeezelite.service <<UNIT
[Unit]
Description=Squeezelite HIFI Endpoint (home-patch)
Wants=network-online.target sound.target
After=network-online.target sound.target

[Service]
User=squeezelite
Group=audio
ExecStart=/usr/bin/squeezelite -n ${PLAYER_NAME} -s ${LMS_IP} -o ${PICK} -a 80:4:: -C 5
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable squeezelite >/dev/null 2>&1 && ok "squeezelite 开机自启已启用" || ng "enable 失败"
systemctl start squeezelite && sleep 3
systemctl is-active --quiet squeezelite && ok "squeezelite 运行中" || ng "squeezelite 未运行(查 journalctl -u squeezelite)"
# SlimProto 注册验证: 与 LMS:3483 的 ESTABLISHED 连接
CONN=$(ss -tn state established 2>/dev/null | grep -c ":3483")
[[ "${CONN:-0}" -ge 1 ]] && ok "SlimProto 已注册到 LMS($LMS_IP:3483)" || ng "未见 LMS 连接(等 30s 再查/查 LMS 日志)"

# ---------------- Phase 4: WOL 持久化 ----------------
info "Phase 4: Wake-on-LAN 持久化"
$ETHTOOL -s "$NET_IF" wol g 2>/dev/null
cat > /etc/systemd/system/wol-enable.service <<UNIT
[Unit]
Description=Persist Wake-on-LAN (home-patch)
After=network.target

[Service]
Type=oneshot
ExecStart=${ETHTOOL} -s ${NET_IF} wol g

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable wol-enable.service >/dev/null 2>&1 && ok "wol-enable.service 已启用" || ng "wol-enable enable 失败"
WOL_NOW=$($ETHTOOL "$NET_IF" 2>/dev/null | grep -i "wake-on" | head -1)
if echo "$WOL_NOW" | tr 'a-z' 'A-Z' | grep -q "G"; then
  ok "WOL=g 生效: $WOL_NOW"
else
  ng "WOL 未生效 — 查 BIOS(Wake on PCIe/Deep Sleep=Disabled); 当前: $WOL_NOW"
fi

# ---------------- Phase 5: CPU performance ----------------
info "Phase 5: CPU governor"
echo 'GOVERNOR="performance"' > /etc/default/cpufrequtils
systemctl restart cpufrequtils >/dev/null 2>&1 || systemctl enable --now cpufrequtils >/dev/null 2>&1
GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)
if [[ "$GOV" == "performance" ]]; then ok "CPU governor=performance"; else ng "governor=$GOV (cpufrequtils 未生效, 可忽略若无该驱动)"; fi

# ---------------- Phase 6: HA 关机通道(ha-admin + sudoers 白名单) ----------------
info "Phase 6: HA 关机通道"
id "$HA_ADMIN" &>/dev/null || useradd -m -s /bin/bash "$HA_ADMIN"
install -d -m 700 -o "$HA_ADMIN" -g "$HA_ADMIN" "/home/$HA_ADMIN/.ssh"
echo "$PUBKEY" > "/home/$HA_ADMIN/.ssh/authorized_keys"
chmod 600 "/home/$HA_ADMIN/.ssh/authorized_keys"
chown "$HA_ADMIN:$HA_ADMIN" "/home/$HA_ADMIN/.ssh/authorized_keys"
echo "$HA_ADMIN ALL=(ALL) NOPASSWD: /usr/sbin/poweroff, /usr/sbin/reboot" > /etc/sudoers.d/90-ha-admin
chmod 440 /etc/sudoers.d/90-ha-admin
if visudo -c >/dev/null 2>&1; then ok "ha-admin + sudoers(poweroff/reboot 免密)就绪"; else ng "sudoers 语法错误! 立即检查 /etc/sudoers.d/90-ha-admin"; fi
systemctl is-active --quiet ssh && ok "sshd 运行中" || { systemctl enable --now ssh >/dev/null 2>&1; systemctl is-active --quiet ssh && ok "sshd 已拉起" || ng "sshd 未运行"; }

# ---------------- Phase 7: 备用渲染服务(预装,停用) ----------------
info "Phase 7: 备用渲染服务(AirPlay/DLNA renderer)"
if apt-get install -y -qq shairport-sync gmrender-resurrect >>"$REPORT" 2>&1; then
  for svc in shairport-sync gmrender-resurrect; do
    systemctl disable --now "$svc" >/dev/null 2>&1
    ST=$(systemctl is-active "$svc" 2>/dev/null)
    [[ "$ST" != "active" ]] && ok "$svc 已预装并停用(备用,需时 enable)" || ng "$svc 意外运行中(与 squeezelite 抢 ALSA)"
  done
else
  ng "备用服务安装失败(不影响主线; Phase F 手动补装)"
fi

# ---------------- 汇总 ----------------
{
  echo "=================================="
  echo "PASS=$PASS  FAIL=$FAIL"
  echo "MAC =$MAC   IP=$CUR_IP  IF=$NET_IF"
  echo ">>> 请在 AX6000 绑定静态租约: 10.10.10.3 <-> $MAC"
  echo ">>> HA 侧待办: wake_on_lan switch(mac=$MAC) + shell_command(ha-admin@10.10.10.3 sudo poweroff)"
  echo ">>> DAC 到货后: aplay -l 重探 -> sed 替换 /etc/systemd/system/squeezelite.service 中 -o 参数 -> daemon-reload && restart"
} | tee -a "$REPORT"
[[ $FAIL -eq 0 ]] && echo "=== ALL GREEN ===" | tee -a "$REPORT"
exit "$FAIL"
