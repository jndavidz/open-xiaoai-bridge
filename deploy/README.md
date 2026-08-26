# 家庭部署手册（deploy/）

> 目标：群晖 DS416play 上运行 open-xiaoai-bridge，小爱 Pro(LX06) 经 client 连接，
> 实现：免唤醒短语场景控制 + 「你好贾维斯」DeepSeek 连续对话 + HA↔音箱双向 TTS。
> 对应总方案：`../../doc/plan/home-theater-automation-plan.md` Phase C/D。

## 1. 前置

- 群晖 SSH：`ssh zxsadmin@10.10.10.2`，docker 全路径 `/usr/local/bin/docker`
- LX06 已刷补丁固件、client-rust 可运行（SSH：`ssh -o HostKeyAlgorithms=+ssh-rsa root@10.10.10.20`，密码 open-xiaoai）
- DeepSeek API Key、HA 长期访问令牌

## 2. 本地准备（WSL2）

```bash
cd /mnt/d/repos/open-xiaoai/bridge/deploy
cp .env.example .env && vim .env      # 填三个 token/key
# 下载模型包（VAD+KWS；xiaoai_asr 模式不需要 ASR 大模型，但包内含，体积可控）
#   URL 见下方"模型包"节，解压到 ./models/
```

## 3. 上传到群晖并启动

```bash
ssh zxsadmin@10.10.10.2 'mkdir -p /volume2/docker/open-xiaoai-bridge'
scp -r config.py docker-compose.yml .env \
    zxsadmin@10.10.10.2:/volume2/docker/open-xiaoai-bridge/
# models/ 若大，先 scp models.zip 再在群晖解压（群晖有 unzip）
scp models.zip zxsadmin@10.10.10.2:/volume2/docker/open-xiaoai-bridge/
ssh zxsadmin@10.10.10.2 'cd /volume2/docker/open-xiaoai-bridge && unzip -o models.zip -d models && /usr/local/bin/docker compose up -d'
```

## 4. LX06 升级 client 并切换 server 指向

> ⚠ 需要把音箱上的 client 从「官方版」升级为「coderzc fork 版」——官方 client 不支持鉴权 token，
> 而 bridge 的 `OPEN_XIAOAI_TOKEN` 需要 client 端携带同值 Bearer（fork 版从 `/data/open-xiaoai/token.txt` 读取）。

```bash
ssh -o HostKeyAlgorithms=+ssh-rsa root@10.10.10.20

# 1) 更新 server 指向群晖 bridge
echo 'ws://10.10.10.2:4399' > /data/open-xiaoai/server.txt

# 2) 写入与 server 端一致的鉴权 token（deploy/.env 里的 OPEN_XIAOAI_TOKEN）
echo '<与.env相同的OPEN_XIAOAI_TOKEN>' > /data/open-xiaoai/token.txt

# 3) 用 fork 版 init.sh 重装 client（官方 init.sh 拉的是官方二进制，无 token 逻辑）
curl -sSfL https://gitee.com/coderzc/open-xiaoai/raw/main/packages/client-rust/init.sh | sh

# 4) 若需开机自启，重下 boot.sh 并重启
curl -L -o /data/init.sh https://gitee.com/coderzc/open-xiaoai/raw/main/packages/client-rust/boot.sh
reboot
```

> 注意：重装后 `/data/open-xiaoai/server.txt` 与 `token.txt` 若被 init.sh 覆盖需重新写入；
> 官方版与 fork 版协议同源（AppMessage 四件套），切换无兼容性风险。

## 5. 验收清单（按序）

1. `curl http://10.10.10.2:9092/api/health` → 200
2. 浏览器打开 **http://10.10.10.2:9092/admin** → 输入 `.env` 里的 `ADMIN_TOKEN` → 总览页各卡片正常（aurora / home-assistant 探测在线）
3. `docker logs -f open-xiaoai-bridge` → 出现音箱连接 + `get_version` 日志
4. 音箱喊 **「测试模式」** → 播报「桥接正常，家庭中枢在线」
5. 音箱喊 **「你好贾维斯」** → 播「我在」→ 问一句天气 → DeepSeek 回答（多轮追问验证上下文；喊「小爱同学」验证可打断）
6. HA 侧建一个临时脚本调 `POST http://10.10.10.2:9092/api/play/text`（body `{"text":"来自HA的播报"}`）→ 音箱说话

## 5.5 后台面板（:9092/admin）

> 无需 SSH 即可查看系统状况与日志、更换上游 AI 的接口地址（含端口）/模型规格/API Key。

- **入口**：`http://10.10.10.2:9092/admin`（局域网 / WireGuard 内可达），首次打开输入 `ADMIN_TOKEN`（存浏览器 localStorage）
- **总览**：bridge 运行态、小爱音箱、AI 对话后端（地址/模型/key 状态）、OpenClaw/QwenPaw、音频管线（VAD/KWS）、外部服务探测（`MONITOR_SERVICES`，默认含 aurora :8080 与 HA）
- **日志**：内存环形缓冲实时增量拉取（约 2000 条），级别过滤、自动滚动
- **设置**：
  - 「测试连接」先预检新 地址+Key（GET /models，兜底 chat ping），再保存
  - 「保存并生效」写入运行时覆盖层 `./data/runtime-overrides.json` 并热生效——下一次对话即用新配置，**无需重启容器**
  - 覆盖值优先级高于 config.py/.env；字段旁有「已覆盖」标记，「清除」回落底层值
- **安全**：所有 `/api/admin/*` 需 Bearer token；未配置 `ADMIN_TOKEN` 时接口整体拒绝；密钥读取只回掩码
- 升级镜像时注意：`upgrade-image.sh` 重启容器不影响 `data/`（覆盖层持久化在宿主卷）

## 6. HA 侧需预建的 script 实体（免唤醒表引用）

| script 实体 | 内容（示例） |
|------------|-------------|
| `script.hifi_mode` | 依次：WOL 唤醒 NUC → 等待 ping 通 → media_player.select_source 或 MA 播放 → （可选）经 :9092 播「高保真模式已开启」 |
| `script.music_stop` | `media_player.media_stop`（MA/LMS player） |
| `script.music_play` | `media_player.media_play` |
| `script.music_next` / `music_prev` | `media_player.media_next_track` / `previous_track` |
| `script.music_vol_up` / `vol_down` | `media_player.volume_up` / `volume_down` |

config.py 只认 script 名，HA 侧改实现不影响语音层。

## 7. 免唤醒词表与调参

- 词表在 `config.py` 的 `APP_CONFIG["wakeup"]["keywords"]`（AI 唤醒词 + 全部免唤醒短语都在此）
- 识别不灵：`kws.keywords_threshold` 下调（0.2→0.1）；误触发：上调 + `vad.threshold` 上调（当前 0.3）
- 短词（4 字以下）易误触发，优先用「下一首歌曲」而非「下一首」

## 8. 安全

- `.env` 不入库；`OPEN_XIAOAI_TOKEN` 已启用（client 连接需携带同值 Bearer）
- `ADMIN_TOKEN` 必须强随机（`openssl rand -hex 16`）；面板可改上游 API 配置，泄露等同泄露 API Key
- 4399/9092 仅在局域网（host 网络 + 群晖防火墙），勿做公网端口映射；外网访问走 WireGuard
- HA Token 只进 `.env`，任何文件不得硬编码

## 模型包

VAD+KWS 模型 release：https://github.com/coderzc/open-xiaoai-bridge/releases/tag/vad-kws-asr-models
（WSL2 下载若慢，可临时走路由器透明代理；下载后 `unzip` 到 `models/`）
