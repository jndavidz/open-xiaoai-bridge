"""CCH 投送通道（T2 ticket 08）—— Songloft 票据 URL 经 /cch/cast 投到 CCH 屏。

设计（用户裁定 2026-11-19）：
- CCH 播控走语音 + bridge 作中心 + 配合 HA；
- 投送路径 = Songloft prepare 出票据 URL → POST NAS /cch/cast → patched dlnaserver → 桥 APK v0.2.0；
- 端侧只接触短时效票据 URL，不接触管理员凭证；
- 换代显式撤票归 bridge（songloft NewEpoch 只 ++ 不撤旧票）。

本模块职责：
1. `resolve_ticket_url(ref)` —— 经 Songloft managed prepare 拿票据化绝对 URL（复用 song_bridge 的 client）；
2. `cast(url)` —— POST NAS /cch/cast（Bearer PANEL_TOKEN + CAST_ALLOW 白名单约束）；
3. `revoke(session, epoch, entry)` —— stop 撤票 / 换代逐条撤。

凭据纪律：PANEL_TOKEN / SONGLOFT_PASSWORD 从配置/env 读，绝不入日志/文档/URL；
日志只记事件与 http 码，不记 token/ticket 值。成功判据一律设备侧 observable（调用方负责回读）。
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

from core.utils.logger import logger

# Songloft managed prepare 轮询上限（与 song_bridge._songloft_resolver 一致 120s）
_PREPARE_TIMEOUT_S = 120
_PREPARE_POLL_S = 2

# 平台角标 logo（§2.77-53/54，2026-11-24 方标本地化）：播控页专辑图左上角显示平台方标（如酷狗蓝 K）。
# 2026-11-24 本地化：6 平台方标下载到 NAS cch-bg 目录，经 9092 免鉴权静态路由访问，
# 避免 alicdn URL 过期。文件名 16 位 hex（bg 路由的格式要求）：
#   0001=QQ 0002=咪咕 0003=酷我 0004=千千 0005=酷狗 0006=网易云
# sourceLogo/sourceLogoColor 同填方标（真机证实左上角渲染方标）。
_BG_LOGO_BASE = "http://10.10.10.2:9092/api/cch/bg/"
_SOURCE_LOGO = {
    "kugou":   (_BG_LOGO_BASE + "0000000000000005.jpg", _BG_LOGO_BASE + "0000000000000005.jpg"),
    "netease": (_BG_LOGO_BASE + "0000000000000006.jpg", _BG_LOGO_BASE + "0000000000000006.jpg"),
    "qq":      (_BG_LOGO_BASE + "0000000000000001.jpg", _BG_LOGO_BASE + "0000000000000001.jpg"),
    "migu":    (_BG_LOGO_BASE + "0000000000000002.jpg", _BG_LOGO_BASE + "0000000000000002.jpg"),
    "kuwo":    (_BG_LOGO_BASE + "0000000000000003.jpg", _BG_LOGO_BASE + "0000000000000003.jpg"),
    "qianqian": (_BG_LOGO_BASE + "0000000000000004.jpg", _BG_LOGO_BASE + "0000000000000004.jpg"),
}


def _cfg(key: str, default=None):
    """读 song_request 配置段（与 song_bridge 同源）。"""
    from core.tools import song_request as sr

    return sr._cfg(key, default)


def cch_cast_url() -> str:
    """NAS /cch/cast 端点（cch-proxy，host 网络 :18080）。"""
    return str(_cfg("cch_cast_url", "http://10.10.10.2:18080/cch/cast")).rstrip("/")


def cast_enabled() -> bool:
    """CCH 主动投送总开关，**默认 False**（票 14 → 16/17 的 D 方向裁决）。

    关闭原因（真机实证）：
    - 真人语音下原厂 AGMC 云链与桥链**并发**，桥链 8/8 次在拿到窗口焦点 81–190 ms
      内被交回（`ws: docs/devices/AliGenie/cch-tg-z02-facts.md` §2.27）；
    - 更严重的是桥的 `BridgeReceiver` MediaPlayer 是**独立第二播放器**，原厂暂停后
      它仍在出声（§2.30 实测 `media.audio_flinger` track 表 Client=com.cch.bridge Active=yes），
      且每次起播泄漏一条音频焦点监听器（累积 17 条）。

    ⇒ D 方向 = 原厂负责出声，bridge 只做控制/状态；投送仅保留代码与日志，默认不执行。
    打开此开关即回滚到 v0.2.0 行为（也须同时回装播放版桥 APK，见 `deploy/cch-music-bridge/README.md`）。
    """
    from core.music.cch_config import cfg_bool

    return cfg_bool(_cfg("cch_cast_enabled", False), False)


def panel_token() -> str:
    """cch-proxy 的 PANEL_TOKEN（env 回落，不入库）。"""
    import os

    val = str(_cfg("cch_panel_token", "") or "")
    return val or os.environ.get("PANEL_TOKEN", "")


async def _songloft_client():
    """复用 song_bridge 的 songloft 客户端（token + resolver），避免重复实现。"""
    from core.music import song_bridge

    return song_bridge


async def resolve_ticket_url(ref: dict, *, session: str, epoch: int, entry: str) -> str:
    """经 Songloft managed prepare 拿票据化绝对 URL。

    ref = {"source":..., "id":..., "extra":...}；返回 base + /media?media_ticket=...。
    失败抛 ManagedError（由调用方收敛为错误串）。
    """
    from core.music.model import ManagedError, TrackRef

    sb = await _songloft_client()
    base = str(sb._songloft_cfg("songloft_base", "") or "").rstrip("/")
    if not base:
        raise ManagedError("cch_cast: songloft_base not configured")
    import aiohttp

    async with aiohttp.ClientSession() as http:
        token = await sb._songloft_token(base, http)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {"session": session, "epoch": epoch, "entry": entry, "ref": ref}
        async with http.post(f"{base}/api/v1/managed/prepare", json=body, headers=headers) as resp:
            if resp.status != 202:
                raise ManagedError(f"cch_cast prepare: http {resp.status}")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _PREPARE_TIMEOUT_S
        reprepared = False
        while loop.time() < deadline:
            await asyncio.sleep(_PREPARE_POLL_S)
            qs = urlencode({"session": session, "epoch": epoch, "entry": entry})
            async with http.get(f"{base}/api/v1/managed/prepare?{qs}", headers=headers) as resp:
                if resp.status == 404 and not reprepared:
                    # songloft 重启/op 丢失（ops 内存态，ready op 不恢复，2026-11-20
                    # 17:44 实测 restarts=2）⇒ 新代次 re-prepare 自愈一次，保 managed 语义。
                    reprepared = True
                    epoch += 1
                    body["epoch"] = epoch
                    async with http.post(f"{base}/api/v1/managed/prepare",
                                         json=body, headers=headers) as rp:
                        if rp.status != 202:
                            raise ManagedError(f"cch_cast re-prepare: http {rp.status}")
                    continue
                if resp.status != 200:
                    raise ManagedError(f"cch_cast prepare poll: http {resp.status}")
                data = await resp.json()
            state = str(data.get("state") or "")
            if state == "ready":
                media = str(data.get("media_url") or "")
                if not media.startswith("/media?"):
                    raise ManagedError("cch_cast: bad media_url")
                logger.info("[CchCast] ticket ready", module="CchCast")
                return base + media
            if state == "failed":
                raise ManagedError(f"cch_cast prepare failed: {data.get('error', '')}")
        raise ManagedError("cch_cast prepare timeout")


async def cast(url: str) -> bool:
    """POST /cch/cast {url}（Bearer PANEL_TOKEN）。返回 HTTP 是否 2xx。

    注意：CAST_ALLOW 白名单在 proxy 侧校验——票据 URL host(10.10.10.2:58091)
    必须在白名单内，否则 proxy 返 403（调用方据此判失败，不误报成功）。
    """
    import aiohttp

    token = panel_token()
    if not token:
        logger.warning("[CchCast] PANEL_TOKEN not configured", module="CchCast")
        return False
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as http:
        async with http.post(cch_cast_url(), json={"url": url}, headers=headers) as resp:
            ok = 200 <= resp.status < 300
            logger.info(f"[CchCast] /cch/cast http={resp.status} ok={ok}", module="CchCast")
            return ok


async def revoke(session: str, epoch: int, entry: str) -> bool:
    """显式撤票：stop 命令带票据 epoch+entry（songloft bff 按此撤销）。"""
    from core.music.model import ManagedError

    sb = await _songloft_client()
    base = str(sb._songloft_cfg("songloft_base", "") or "").rstrip("/")
    if not base:
        return False
    import aiohttp

    async with aiohttp.ClientSession() as http:
        token = await sb._songloft_token(base, http)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {"name": "stop", "session": session, "epoch": epoch, "entry": entry}
        async with http.post(f"{base}/api/v1/managed/command", json=body, headers=headers) as resp:
            # songloft Execute 顺序：bff.Execute(stop→RevokeEntry 撤票) 在前，
            # forwardCommand(通知 bridge 托管会话) 在后。CCH 投送无桥托管会话 ⇒
            # 转发失败返 502，但票据已撤。502 因此视为"撤票已生效"的次生错误。
            ok = (200 <= resp.status < 300) or resp.status == 502
            logger.info(
                f"[CchCast] revoke entry={entry} http={resp.status} ok={ok}"
                + (" (502: ticket revoked, bridge forward failed)" if resp.status == 502 else ""),
                module="CchCast",
            )
            return ok


# ---- D 方向点播生产链（票 15/17，2026-11-20）：songloft 票据 → renderData(28 字段)
#      → cch-tts :9102 POST /cmd COMMAND 7（桥 v0.4.4 内 stop→push→重连→play）。
#      旧 /cch/cast 投桥第二播放器链保留在上文（回滚用），默认关闭（cast_enabled=False）。

def np_enabled() -> bool:
    """D 方向 kwqq 点播总开关（cch_np_enabled，默认 False——部署窗口由用户打开）。

    True 时 song_request target=cch 走「票据 URL + kwqq 元数据 → renderData →
    9102 /cmd COMMAND 7」生产链；False 时维持票 16/17 的关闭语义（原厂云链自行处理）。
    """
    from core.music.cch_config import cfg_bool

    return cfg_bool(_cfg("cch_np_enabled", False), False)


def tts_base() -> str:
    """cch-tts :9102 基址（capture 容器 host 网络）。"""
    return str(_cfg("cch_tts_base", "http://10.10.10.2:9102")).rstrip("/")


_token_warned = False


def tts_token() -> str:
    """tts_http 的 Bearer（SPEAK_TOKEN 或 BRIDGE_TOKEN 同源）。

    缺失时**必须显式告警一次**：2026-11-21 事故（部署漏 BRIDGE_TOKEN）表现为
    musicctl/speak 全部在客户端前置失败，外部只看到「设备未就绪」，排查成本极高。
    """
    import os

    global _token_warned
    val = str(_cfg("cch_tts_token", "") or "")
    token = val or os.environ.get("BRIDGE_TOKEN", "")
    if not token and not _token_warned:
        _token_warned = True
        logger.warning(
            "[CchCast] tts token missing (config cch_tts_token / env BRIDGE_TOKEN); "
            ":9102 calls (speak/musicctl/hostcmd) will fail",
            module="CchCast")
    return token


def inject_lyric(render_data: dict, song_id: str) -> dict:
    """把本曲 lrc URL 注入 renderData（纯函数，不原地改写入参）。

    换代（cch_np._cast_entry）必须重新注入：设备显示层只在收到 renderData 时更新
    歌词/封面，否则换曲后沿用上一首的 lrc（2026-11-26 用户报「歌词对不上声音」）。
    """
    url = f"http://127.0.0.1:18081/{song_id}.lrc"
    out = dict(render_data)
    out["lyricUrl"] = url
    try:
        ext = json.loads(out.get("audioExt") or "{}")
        if not isinstance(ext, dict):
            ext = {}
    except (TypeError, ValueError):
        ext = {}
    ext["lyric"] = url
    out["audioExt"] = json.dumps(ext, ensure_ascii=False)
    return out


async def prepare_background(cover: str) -> str:
    """封面 → 服务端虚化（含自适应黑罩）→ 只读 URL（点播/换代共用）。

    失败回退空串，由 build_render_data 回落清晰封面（2026-11-26 用户报
    「换代后全屏背景没虚化」——旧换代链未生成 bg_url）。
    """
    if not cover:
        return ""
    try:
        import aiohttp

        from core.music import bg_cache

        async with aiohttp.ClientSession() as http:
            return str(await bg_cache.ensure(cover, base=bg_base(), http=http) or "")
    except Exception as exc:  # noqa: BLE001 - 背景失败不阻塞播放
        logger.warning(f"[CchCast] bg blur failed: {type(exc).__name__}", module="CchCast")
        return ""


async def write_lyric(item: dict) -> bool:
    """只取词并写设备本地 lrc 文件，不触碰 renderData（呈现层的独立副作用）。

    与 attach_lyric 的区别：本函数不返回被修改的 renderData，调用方可在
    唯一一次 renderDisplay 之后异步写歌词，避免设备 UI 连续重载两次。
    """
    import aiohttp

    sid = str(item.get("id") or "")
    if not sid:
        return False
    lrc = ""
    try:
        from core.tools.song_request import _lyric_has_content

        base = str(_cfg("api_base", "http://10.10.10.2:3003")).rstrip("/")
        async with aiohttp.ClientSession() as http:
            try:
                async with http.get(
                    f"{base}/{str(item.get('source') or 'kuwo').lower()}/lyric",
                    params={"id": sid}, timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        if isinstance(body, dict) and body.get("code") == 200:
                            lrc = str((body.get("data") or {}).get("lyric") or "")
            except Exception:  # noqa: BLE001
                pass
            if not _lyric_has_content(lrc):
                try:
                    from core.music import songloft_api

                    ly = await songloft_api.lyric(
                        str(item.get("name") or ""),
                        str(item.get("artist") or item.get("singer") or ""), http=http)
                    cross = songloft_api.pick_lrc(ly)
                    if _lyric_has_content(cross):
                        lrc = cross
                except Exception:  # noqa: BLE001
                    pass
        if lrc:
            ok, _ = await musicctl("lyric", {"id": sid, "lrc": lrc})
            if ok:
                return True
            logger.warning(f"[CchCast] lyric push failed id={sid}", module="CchCast")
        else:
            logger.info(f"[CchCast] lyric empty id={sid}", module="CchCast")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[CchCast] write_lyric err: {type(exc).__name__}", module="CchCast")
    return False


async def attach_lyric(item: dict, render_data: dict) -> dict:
    """取词 → 写设备 → 注入 renderData（点播与换代共用；失败不抛、不阻塞播放）。

    同平台精确（按音源 id，不硬指定平台）→ Songloft 跨源回搜兜底，只认带时间轴
    的有效 lrc（_lyric_has_content），避免空壳覆盖旧歌词。
    """
    import aiohttp

    sid = str(item.get("id") or "")
    if not sid:
        return render_data
    lrc = ""
    via = ""
    try:
        from core.tools.song_request import _lyric_has_content  # 延迟 import 破循环

        base = str(_cfg("api_base", "http://10.10.10.2:3003")).rstrip("/")
        async with aiohttp.ClientSession() as http:
            try:
                async with http.get(
                    f"{base}/{str(item.get('source') or 'kuwo').lower()}/lyric",
                    params={"id": sid}, timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        if isinstance(body, dict) and body.get("code") == 200:
                            lrc = str((body.get("data") or {}).get("lyric") or "")
                            via = "same-platform" if lrc else ""
            except Exception:  # noqa: BLE001 - 同平台失败回落跨源
                pass
            if not _lyric_has_content(lrc):
                cross = ""
                try:
                    from core.music import songloft_api

                    ly = await songloft_api.lyric(
                        str(item.get("name") or ""),
                        str(item.get("artist") or item.get("singer") or ""), http=http)
                    cross = songloft_api.pick_lrc(ly)
                except Exception:  # noqa: BLE001 - 跨源失败保留原值
                    pass
                if _lyric_has_content(cross):
                    lrc = cross
                    via = "songloft-cross"
                elif not lrc:
                    lrc = cross
                    via = "songloft-cross-raw"
        if lrc:
            ok, _ = await musicctl("lyric", {"id": sid, "lrc": lrc})
            if ok:
                render_data = inject_lyric(render_data, sid)
                logger.info(f"[CchCast] lyric attached id={sid} via={via or 'unknown'} "
                            f"len={len(lrc)}", module="CchCast")
            else:
                logger.warning(f"[CchCast] lyric push failed id={sid} via={via}", module="CchCast")
        else:
            logger.info(f"[CchCast] lyric empty id={sid}", module="CchCast")
    except Exception as exc:  # noqa: BLE001 - 歌词失败不阻塞播放
        logger.warning(f"[CchCast] lyric err: {type(exc).__name__}", module="CchCast")
    return render_data


async def render_display(render_data: dict) -> bool:
    """显示补投：纯 VIEW intent renderData（不带播控副作用），刷新歌词/封面。"""
    try:
        ok, _ = await musicctl("render", {"renderData": json.dumps(
            render_data, ensure_ascii=False)})
        return bool(ok)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[CchCast] render_display err: {type(exc).__name__}", module="CchCast")
        return False


async def kwqq_meta(song_id: str, source: str = "kuwo") -> dict:
    """门面曲目元数据（envelope 契约）。失败返回空 dict（renderData 退化用 id）。

    `source` 必须是被点曲目的实际源（Songloft 聚合后常为 kugou/netease 等），
    固定 `/kuwo/` 会拿不到 id（票 24 review 修复）。
    """
    import aiohttp

    base = str(_cfg("api_base", "http://10.10.10.2:3003")).rstrip("/")
    src = str(source or "kuwo").strip().lower() or "kuwo"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(
                f"{base}/{src}/song/info", params={"id": song_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return {}
                body = await resp.json()
        if isinstance(body, dict) and body.get("code") == 200:
            return body.get("data") or {}
        return {}
    except Exception as exc:  # noqa: BLE001 —— 元数据失败不阻塞点播
        logger.warning(f"[CchCast] kwqq_meta({src}/{song_id}) err: {type(exc).__name__}", module="CchCast")
        return {}


def _display_audio_name(name: str, singer: str, album: str = "") -> str:
    """播控页主标题 =「歌名 - 歌手」（2026-11-24 用户本轮裁决，取代当日早先的
    「歌手 - 歌名」版；audioName 同字段也渲染在全屏歌词页左下角，两处随之同步）。
    原厂标题控件不渲染 \n（注入被吞成空格），故单行；缺失其一则只显示另一项。
    album 参数保留兼容调用点，但不拼进标题。"""
    n = str(name or "").strip()
    s = str(singer or "").strip()
    return f"{n} - {s}" if (n and s) else (n or s)


def build_render_data(ticket_url: str, item: dict, meta: dict,
                      background_image: str = "") -> dict:
    """构造原厂 MusicService parseIntent 的 renderData（28 字段，facts §2.34 已验字段集）。

    item = song_request 搜索项（name/artist/album/cover/source/id）；
    meta = kwqq_meta() 载荷（name/singer/album/cover/duration 优先级更高）。
    audioUrl 只进 short-lifetime 票据 URL（facts §2.34：端侧零凭证）。
    """
    name = str(meta.get("name") or item.get("name") or "歌曲")
    singer = str(meta.get("singer") or item.get("artist") or "")
    album = str(meta.get("album") or item.get("album") or "")
    cover = str(meta.get("cover") or item.get("cover") or "")
    # kwqq-api /{source}/song/info 返回的是 duration_s（秒），没有 duration 字段
    # （真机实测 2026-11-24：kugou 源 duration_s=299、duration 缺失）；只认 duration
    # 会让 audioLength 写死 0、屏上不显示时长（§2.77-49）。按
    # meta.duration > meta.duration_s > item.duration > item.duration_s 优先级兜底，
    # 原厂 audioLength 单位 = 秒（facts L486：319=5:19）。
    raw_duration = (meta.get("duration") or meta.get("duration_s")
                    or item.get("duration") or item.get("duration_s") or 0)
    try:
        duration = max(0, int(float(raw_duration)))  # 字符串浮点兼容 + 负数钳零
    except (TypeError, ValueError):
        duration = 0
    sid = str(meta.get("id") or item.get("id") or "")
    # 平台角标（§2.77-53）：按 item 真实平台（门面 source_data.s）填 logo；
    # renderData 的 source/audioSource 仍写 KWQQ（内核标识，无白名单校验，不能动）。
    source_logo, source_logo_color = _SOURCE_LOGO.get(
        str(item.get("source") or "").strip().lower(), ("", ""))
    audio_ext = json.dumps({
        "audioAnchor": singer, "audioType": "music", "audioSource": "KWQQ",
        "EnableNetDX": "true", "identifyStatus": 0, "playSpeedStatus": 0,
        "request_id": f"bridge-{sid or 'np'}",
    }, ensure_ascii=False, separators=(",", ":"))
    return {
        "climaxEnd": 0, "audioId": sid, "audioSource": "KWQQ",
        "pageSize": 30, "playFrom": "bridge.np.1", "source": "KWQQ",
        "playlistOnly": False, "playMode": "Normal",
        "cover": {"img": cover}, "defaultPlayOrder": 0,
        "totalEpisode": 0, "audioUrl": ticket_url, "appId": "200",
        "audioAlbum": album, "rawId": sid, "soundQuality": "高音质",
        "audioImageUrl": cover, "copyRight": 0,
        # 全屏歌词页/播控页的整屏背景：优先用服务端虚化图（bg_cache），否则回退封面。
        # 云端 ContentPlay 会带专门的艺术背景图；我们无该素材，用虚化封面替代
        # （用户口径 2026-11-21：背景应为虚化效果，不是清晰专辑图）。
        "backgroundImage": str(background_image or cover), "lyricbackGroundAuhor": "",
        "itemRawId": sid, "audioUid": sid, "itemId": sid,
        "toast": "", "audioName": _display_audio_name(name, singer, album),
        "collect": False, "template": "",
        "albumName": album, "audioExt": audio_ext,
        "requestType": "create", "itemType": "music",
        # 平台角标：真机原厂曲按平台下发这两个字段，播控页封面左上角显示平台标。
        "sourceLogo": source_logo, "sourceLogoColor": source_logo_color,
        "audioLength": duration,
    }


async def push_queue(items: list) -> tuple[bool, str]:
    """把门面搜索结果 topN 元数据经 hostcmd queue 写宿主 UI 队列（票 21）。

    载荷由门面统一构造（`songloft_api.queue_payload`）——平台字段取门面 source_data.s，
    与 songloft App 经 kwqq 插件消费同一标识，不在投送层另建映射。保持搜索原序整批
    replace；原厂列表高亮是否自动跟随播放曲待真机窗口验证（票 21 Comments 挂账）。
    失败不抛（队列是体验增强，不阻塞起播）。
    """
    from core.music import songloft_api

    meta = songloft_api.queue_payload(items)
    if not meta or meta == "[]":
        return False, "empty"
    ok, status = await host_cmd("queue", extra=meta)
    logger.info(f"[CchCast] hostcmd queue n={len(items)} ok={ok} status={status}",
                module="CchCast")
    return ok, status


async def push_favorites(playlist_id=None) -> tuple[bool, dict]:
    """读 Songloft 收藏歌单 → 映射原厂 item schema → 经 9102 favfile 落设备文件（票 25）。

    唯一真相在 Songloft（票 13）；本函数只做「读 → 映射 → 下发」，不在投送层另建映射。
    playlist_id 缺省 = 内置收藏歌单。下发失败不抛（收藏写已成功，文件同步是增强），
    由调用方决定是否重试。
    """
    import aiohttp

    from core.music import songloft_api

    try:
        async with aiohttp.ClientSession() as http:
            pl = int(playlist_id) if playlist_id is not None else await songloft_api.favorites_playlist_id(http=http)
            songs = await songloft_api.playlist_songs(pl, http=http)
    except Exception as exc:  # noqa: BLE001 - Songloft 读失败，明确返回不抛
        logger.warning(f"[CchCast] push_favorites read failed: {type(exc).__name__}: {exc}",
                       module="CchCast")
        return False, {"error": f"read:{type(exc).__name__}"}
    items = songloft_api.favorite_item_schema(songs)
    ok, payload = await musicctl("favfile", {"items": items})
    logger.info(f"[CchCast] favfile n={len(items)} playlist={pl} ok={ok} status={payload}",
                module="CchCast")
    # 票 01h：收藏变化后同步刷新历史 tab 红心（历史 item 的 collect 由收藏真相源校准）。
    # fire-and-forget，失败不影响收藏写入本身（fail-open）。
    if ok:
        from core.music import cch_history

        asyncio.ensure_future(cch_history._push())
    return ok, {"n": len(items), "playlist_id": pl, "ok": ok, "detail": payload}


async def cast_native(render_data: dict) -> tuple[bool, str]:
    """POST 9102 /cmd COMMAND 7（Bearer）。返回 (ok, detail)——ok = HTTP 2xx，
    生效与否看设备侧 observable（CchBridgeMeta/audio_flinger），调用方负责回读。"""
    import aiohttp

    token = tts_token()
    if not token:
        logger.warning("[CchCast] tts token not configured (BRIDGE_TOKEN)", module="CchCast")
        return False, "tts token not configured"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"command": 7, "extras": {"renderData": json.dumps(render_data, ensure_ascii=False)}}
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{tts_base()}/cmd", json=body, headers=headers) as resp:
            ok = 200 <= resp.status < 300
            logger.info(f"[CchCast] /cmd c=7 http={resp.status} ok={ok}", module="CchCast")
            return ok, f"http {resp.status}"


async def cast_native_simple(command: int) -> tuple[bool, str]:
    """播控转发（COMMAND 2/3/8）与收藏（9）经 9102 /cmd。"""
    import aiohttp

    if command not in (2, 3, 6, 8, 9):
        return False, "command not allowed"
    token = tts_token()
    if not token:
        return False, "tts token not configured"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"command": command, "extras": {}}
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{tts_base()}/cmd", json=body, headers=headers) as resp:
            ok = 200 <= resp.status < 300
            logger.info(f"[CchCast] /cmd c={command} http={resp.status} ok={ok}", module="CchCast")
            return ok, f"http {resp.status}"


# ---- A3-4 起播前编排（facts §2.48：续播归因 native 残留队列 / 重注册外部路径） ----
_last_core_pid: int = 0


def m5_enabled() -> bool:
    """M5 Zygisk 宿主控制通道开关（默认 False；刷机 6 项验收通过后由用户打开）。"""
    from core.music.cch_config import cfg_bool

    return cfg_bool(_cfg("cch_m5_enabled", False), False)


def host_derived_key() -> str:
    """HMAC-SHA256(BRIDGE_TOKEN, salt) hex —— 与 dex TokenAuth 同派生（salt 定死常量）。

    token 缺失返回空串（**不可用空 key 派生**：HMAC("" ,salt) 是公开可算的，
    等于把 hostcmd 通道令牌公开）。
    """
    import hashlib
    import hmac
    token = tts_token()
    if not token:
        return ""
    return hmac.new(token.encode(), b"cch-m5-auth-v1", hashlib.sha256).hexdigest()


async def host_token_deploy() -> tuple[bool, dict]:
    """运行时部署派生 key 到设备内部目录（9102 hosttoken op 走 su = root 域）。幂等。"""
    key = host_derived_key()
    if not key:
        return False, {"error": "NO_TOKEN"}
    return await musicctl("hosttoken", {"key": key})


async def host_cmd(cmd: str, url: str = None, extra: str = None,
                   timeout_s: float = 5.0) -> tuple[bool, str]:
    """M5 控制通道命令（broadcast fire-and-forget + last_result cmd_id 回读）。

    返回 (ok, status)：status = OK / NEED_M5 / AUTH_FAILED / INVALID_CMD / TIMEOUT。
    cmd_id 不匹配 = 广播丢失或处理中；匹配但 AUTH_FAILED = token 不匹配。
    """
    import asyncio
    import uuid
    cmd_id = uuid.uuid4().hex[:12]
    token = host_derived_key()
    if not token:
        return False, "NO_TOKEN"   # 缺 BRIDGE_TOKEN：在客户端前置拒绝，不发明文空 key
    body = {"cmd": cmd, "token": token, "cmd_id": cmd_id}
    if url:
        body["url"] = url
    if extra:
        body["extra"] = extra
    ok, payload = await musicctl("hostcmd", body)
    if not ok:
        if str((payload or {}).get("error") or "") == "tts token not configured":
            return False, "NO_TOKEN"
        return False, "NEED_M5"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        ok2, payload = await musicctl("hostresult", {})
        result = str(payload.get("result") or "")
        if cmd_id in result:
            if '"status":"OK"' in result:
                return True, "OK"
            for st in ("AUTH_FAILED", "INVALID_CMD", "NOT_REGISTERED"):
                if st in result:
                    return False, "NEED_M5" if st == "NOT_REGISTERED" else st
            return False, "NEED_M5"
    # broadcast fire-and-forget 间歇丢（rc=0 但 receiver 无 recv，2026-11-20 实测）⇒ 重发一次
    ok, _ = await musicctl("hostcmd", body)
    if not ok:
        return False, "NEED_M5"
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        ok2, payload = await musicctl("hostresult", {})
        result = str(payload.get("result") or "")
        if cmd_id in result:
            if '"status":"OK"' in result:
                return True, "OK"
            return False, "NEED_M5"
    return False, "TIMEOUT"


async def wait_np_play(timeout_s: float) -> bool:
    """事后验证（cast_need_m5 判据）：timeout 内 np STATE 更新为 play = 起播生效。
    基线 P95 待采（a5/a7 样本 <10），未标定前 cast 轨暂用 8s 保守值（调用方传入）。"""
    import asyncio
    from core.music import cch_np
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    base_ts = (cch_np.snapshot().get("state") or {}).get("device_ts")
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        st = cch_np.snapshot().get("state") or {}
        if st.get("device_ts") != base_ts and str(st.get("extras", {}).get("statusStr")).lower() == "play":
            return True
    return False


async def musicctl(op: str, body: dict = None) -> tuple[bool, dict]:
    """9102 /musicctl 白名单 op（prestop/reinit/corepid/playlist/control/hostcmd/
    hostresult/hosttoken）。返回 (ok, payload)。"""
    import aiohttp

    token = tts_token()
    if not token:
        return False, {"error": "tts token not configured"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{tts_base()}/musicctl", json={"op": op, **(body or {})}, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            ok = 200 <= resp.status < 300
            try:
                payload = await resp.json()
            except Exception:  # noqa: BLE001 - 非 JSON 响应收敛空 payload
                payload = {}
            logger.info(f"[CchCast] /musicctl op={op} http={resp.status} ok={ok}", module="CchCast")
            return ok, payload if isinstance(payload, dict) else {}


async def prepare_playback() -> dict:
    """起播前编排：prestop(22) 清 native 残留队列 → :core pid 变化则 kill :business 重注册。

    - prestop = directStop：清残留续播源且无 isQuitting 副作用（18 stop 弃用，§2.48③）；
    - reinit 条件触发（pid 变化）：kill 后 service 自动重启、静态清零（含 stale 注册与
      isQuitting），initPlayer 由后续 cast 的 VIEW intent 完成（§2.48⑥）；不每次 kill，
      避免曲间切换把用户踢出歌词页；
    - 任一 op 失败不阻塞 cast（降级原链，调用方只记日志）。
    """
    global _last_core_pid
    result = {"prestop": False, "reinit": False, "core_pid": 0}
    ok, _ = await musicctl("prestop")
    result["prestop"] = ok
    ok, payload = await musicctl("corepid")
    pid = int(payload.get("core_pid") or 0)
    result["core_pid"] = pid
    if ok and pid and _last_core_pid and pid != _last_core_pid:
        ok2, _ = await musicctl("reinit")
        result["reinit"] = ok2
        logger.info(
            f"[CchCast] core pid changed {_last_core_pid}->{pid} reinit={ok2}", module="CchCast")
    if pid:
        _last_core_pid = pid
    return result


async def speak(text: str) -> tuple[bool, str]:
    """9102 /speak 原厂 TTS 播报（票 04 下行面）。

    原厂把正在播的 kwqq suspend（STATE 15）；恢复编排归 cch_np.speak_with_resume
    （桥内定时器，非轮询）。日志只记文本长度不记内容（播报文案可能含个人信息）。
    """
    import aiohttp

    token = tts_token()
    if not token:
        return False, "tts token not configured"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{tts_base()}/speak", json={"text": text, "is_open_mic": False},
            headers=headers,
        ) as resp:
            ok = 200 <= resp.status < 300
            logger.info(f"[CchCast] /speak http={resp.status} len={len(text or '')} ok={ok}",
                        module="CchCast")
            return ok, f"http {resp.status}"


def bg_base() -> str:
    """虚化背景图的对外基址（设备侧直取，默认走 bridge API 的 NAS 地址）。"""
    return str(_cfg("cch_bg_base", "http://10.10.10.2:9092")).rstrip("/")


async def host_cmd_nowait(cmd: str, url: str = None, extra: str = None) -> bool:
    """fire-and-forget 版 hostcmd（不轮询 last_result）——用于「立即暂停」这类不需要回执的动作。"""
    import uuid

    token = host_derived_key()
    if not token:
        return False
    body = {"cmd": cmd, "token": token, "cmd_id": uuid.uuid4().hex[:12]}
    if url:
        body["url"] = url
    if extra:
        body["extra"] = extra
    ok, _ = await musicctl("hostcmd", body)
    return ok
