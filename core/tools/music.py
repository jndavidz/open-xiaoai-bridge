"""工具：查询 Lyrion Music Server (LMS) 播放状态（只读）。

经 LMS JSONRPC（默认 http://10.10.10.2:9000/jsonrpc.js）。
返回当前播放曲目的精简摘要，用于回答『现在播的什么歌』『音乐在放吗』。
不控制播放（播放/暂停/切歌走 HA script，见 DIRECT_COMMANDS）。
"""

import os

import aiohttp

from core.utils.logger import logger

NAME = "music_status"

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "查询家庭音乐服务器(LMS)当前播放状态（只读）：正在播放的曲目、艺术家、"
            "播放模式、音量。用于回答『现在播的什么』『音乐在放吗』。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


def _lms_url() -> str:
    host = os.environ.get("LMS_HOST", "10.10.10.2")
    return f"http://{host}:9000/jsonrpc.js"


async def _rpc(method: str, params: list) -> dict | None:
    payload = {"id": 1, "method": "slim.request", "params": params}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _lms_url(), json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status >= 400:
                    return None
                data = await resp.json()
                return data.get("result")
    except Exception as exc:
        logger.error(f"[tools/music] {type(exc).__name__}: {exc}")
        return None


async def run(args: dict) -> str:
    players = await _rpc("players", ["0", "100"])
    if not players:
        return "音乐服务器无响应（可能未开机或未部署 LMS）"
    plist = players.get("players_loop", []) if isinstance(players, dict) else []
    if not plist:
        return "音乐服务器在线，但无已连接播放器"
    # 取第一个播放器
    player = plist[0]
    pid = player.get("playerid") or player.get("ip")
    status = await _rpc(pid, ["status", "-", "1", "tags:xacgt"])
    if not status:
        return f"播放器 {player.get('name', pid)} 状态获取失败"
    # LMS 字段名：title/artist/album/modename/volume
    title = status.get("title") or status.get("current_title")
    artist = status.get("artist")
    mode = status.get("modename")
    vol = status.get("volume")
    if not title and not artist:
        return f"播放器 {player.get('name', pid)} 当前空闲（未在播放）"
    parts = [f"《{title}》" if title else "（未知曲目）"]
    if artist:
        parts.append(f"演唱：{artist}")
    if mode:
        parts.append(f"模式：{mode}")
    if vol is not None:
        parts.append(f"音量：{vol}")
    return "正在播放 " + "，".join(parts)
