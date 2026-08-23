# 家庭部署手册（deploy/）

> 目标：群晖 DS416play 上运行 open-xiaoai-bridge，小爱 Pro(LX06) 经 client 连接，
> 实现：免唤醒短语场景控制 + 「你好贾维斯」DeepSeek 连续对话 + HA↔音箱双向 TTS。
> 对应总方案：`../../doc/home-theater-automation-plan.md` Phase C/D。

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

## 4. LX06 切换 server 指向

```bash
ssh -o HostKeyAlgorithms=+ssh-rsa root@10.10.10.20
echo 'ws://10.10.10.2:4399' > /data/open-xiaoai/server.txt
reboot
```

## 5. 验收清单（按序）

1. `curl http://10.10.10.2:9092/api/health` → 200
2. `docker logs -f open-xiaoai-bridge` → 出现音箱连接 + `get_version` 日志
3. 音箱喊 **「测试模式」** → 播报「桥接正常，家庭中枢在线」
4. 音箱喊 **「你好贾维斯」** → 播「我在」→ 问一句天气 → DeepSeek 回答（多轮追问验证上下文；喊「小爱同学」验证可打断）
5. HA 侧建一个临时脚本调 `POST http://10.10.10.2:9092/api/play/text`（body `{"text":"来自HA的播报"}`）→ 音箱说话

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
- 4399/9092 仅在局域网（host 网络 + 群晖防火墙），勿做公网端口映射
- HA Token 只进 `.env`，任何文件不得硬编码

## 模型包

VAD+KWS 模型 release：https://github.com/coderzc/open-xiaoai-bridge/releases/tag/vad-kws-asr-models
（WSL2 下载若慢，可临时走路由器透明代理；下载后 `unzip` 到 `models/`）
