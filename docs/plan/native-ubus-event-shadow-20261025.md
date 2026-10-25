# native ubus 播放事件源影子比对报告（票 03，2026-10-25）

> 设计：`ws: .scratch/lx06-voice-hub-followups/notes/03-native-ubus-event-source.md`（路径 D：SSH 中继侧车）。
> 本次验证会话已完成并**全部还原**：生产 `native_cmd=""`、`shadow_sources=["client"]`、侧车已停。

## 0. 结论（先读）

| 判据 | 结果 |
|---|---|
| 唯一未验假设：`ubus listen` 管道即时性 | ✅ **即时**（实测自带行缓冲，无需 stdbuf——设备上也没有 stdbuf） |
| 20 正常播报 | ✅ 16/20 三源干净配对；native 与 client 时间线互相印证 |
| 带符号 Δ（native−context） | 起点 P50 **−28.1ms** / P95 −15.3ms；完成 P50 **−102.3ms** / P95 −38.6ms（最优 −210ms）——事件源比 200ms 轮询早，方向符合预期 |
| 4/20 未配对 | ⚠️ **设备侧同时不发 `status:0`**（client 与 native 双源一致缺失，互相印证为设备事件丢失而非桥缺陷）⇒ **超时兜底必须永久保留**（设计判断成立） |
| 异常 3 项 | 中断→native 110ms 收到结束事件（Δ −1113ms，可解释 mismatch）；不可达 URL→接受且不挂起；杀侧车→`native event stream ended; completion falls back to timeout` 兜底生效，无挂起 |
| mismatch_count | 20 正常段累计仅 1（启动竞态一次），无新增 |

**建议**：native 源可用且延迟更优，但生产默认**暂不切**——需常驻第二容器（侧车）且生命周期不同步；
若未来切默认，按设计走路径 A（并入主镜像）。当前生产保持 `native_cmd=""`。

## 1. 验证现场（可复跑）

- 侧车（NAS）：`bridge: deploy/ubus-sidecar.py`（python3 TCP:9100 泵 `ssh LX06 ubus listen`）。
- bridge 配置（热重载）：`event_plane.shadow_sources=["client","native"]`、
  `native_cmd="bash -c 'exec 3<>/dev/tcp/127.0.0.1/9100; cat <&3'"`（host 网络容器内 /dev/tcp 消费）。
- 采集：20 次 blocking `/api/play/text`（3–5s 文本串行）+ 每次 1.5s 后抓 `/api/health.playback`；
  异常 3 项（中断 / 不可达 URL / 杀侧车）。
- 原始数据：`dev: reverse/lx06-firmware-reverse/run/ubus-native-event-shadow-20261025.jsonl`（20+3 行）。

## 2. 过程中的坑（全部实锚）

1. **busybox `timeout` 语法是 `-t SEC`**：`timeout 25 ubus listen` 会去 exec "25" 直接失败——
   前几轮探针空跑全是它（设备 busybox 1.27）。
2. **设备无 stdbuf**：`which stdbuf` rc=1；实测 `ubus listen` 本身行缓冲，无需替代。
3. **`ubus listen` 不能带事件名过滤**：`ubus listen play_status_broadcast_event` 零输出；
   已验证口径是裸 `ubus listen`（事实层 ubus-events-play-status.md）。
4. **🔴 NAS 上改 config.py 的两条铁律（本轮两次事故的根因）**：
   - **禁用 `sed -i`**：config.py 是 bind mount，sed -i 换 inode 后容器永远读旧文件
     （mTime 变了但容器不可见；需 `cat x.bak > config.py` 保 inode 写入，或 recreate）；
   - **禁用 `sed config.py | cat > config.py` 自读自写**：cat 先截断、sed 后读到 EOF ⇒ 写回**空文件**
     ⇒ 容器 crash-loop `config.APP_CONFIG must be a dict`（本次生产中断 ~5 分钟，恢复=从备份 cat 恢复）。
5. DSM `ps -ef`/`/proc/net/tcp` 检查进程与端口（DSM 无 `-w` ps、netstat 可能缺）；
   NAS `pkill -f` 会匹配会话自身命令行，用 `[r]` 括号技巧或分两条命令执行。

## 3. 后续

- 事件面重构引用本报告的 Δ 数据与「4/20 设备丢结束事件」事实（架构判断：事件总线需要
  超时兜底 + 事件可丢失的补偿设计）。
- 生产切换前需要：路径 A（镜像并入）+ 侧车生命周期方案 + 重启丢失窗口评估（票 05 相邻）。
