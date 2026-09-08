"""工具：查询 Home Assistant 实体状态（只读）。

凭据走环境变量（与 deploy/config.py 一致）：HA_BASE_URL / HA_TOKEN。
不修改任何实体——纯 GET /api/states。写操作（开灯/场景）本期未接，
见 runbook T8.4（需二次确认）。
"""

import os

import aiohttp

from core.utils.logger import logger

NAME = "hass_get_states"

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "查询 Home Assistant 中实体当前状态（只读）。"
            "传 entity_id 精确查询单个实体；省略则返回在线实体数量与激活(on/playing)摘要。"
            "用于回答『现在客厅灯亮着吗』『空调设到几度』之类问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "可选，HA 实体 id，如 light.living_room / climate.bedroom_ac；省略则返回摘要",
                }
            },
            "required": [],
        },
    },
}


def _base() -> str:
    return os.environ.get("HA_BASE_URL", "http://10.10.10.2:8123").rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('HA_TOKEN', '')}", "Content-Type": "application/json"}


async def run(args: dict) -> str:
    eid = (args.get("entity_id") or "").strip()
    try:
        async with aiohttp.ClientSession() as session:
            if eid:
                async with session.get(
                    f"{_base()}/api/states/{eid}", headers=_headers(),
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status >= 400:
                        return f"HA 错误 {resp.status}：实体 {eid} 可能不存在或无权限"
                    st = await resp.json()
                    attrs = st.get("attributes", {}) or {}
                    picked = {k: attrs.get(k) for k in
                              ("friendly_name", "media_title", "media_artist",
                               "volume_level", "current_temperature", "temperature")
                              if k in attrs}
                    return f"{eid}: state={st.get('state')}, 关键属性={picked}"
            else:
                async with session.get(
                    f"{_base()}/api/states", headers=_headers(),
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status >= 400:
                        return f"HA 错误 {resp.status}：无法列出实体"
                    states = await resp.json()
                    total = len(states)
                    active = [s.get("entity_id") for s in states
                              if str(s.get("state")) in ("on", "playing", "open")]
                    return f"HA 共 {total} 个实体，其中激活(on/playing/open) {len(active)} 个；示例：{active[:8]}"
    except Exception as exc:
        logger.error(f"[tools/hass] {type(exc).__name__}: {exc}")
        return f"HA 查询失败：{exc}"
