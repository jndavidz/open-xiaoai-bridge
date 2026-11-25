"""工具：收藏/取消收藏 CCH 正在播放的歌（票 13，Songloft 单落点）。

触发：CCH 上说「收藏这首歌」「加入我的歌单」（intent=true）或
「取消收藏」（intent=false）。落点 = Songloft 内置收藏歌单（id=1），
按插件引用入库；不写原厂账号、不调 COMMAND 9（票 13 用户裁决）。
当前曲来源 = `song_request._cch_state` 队列当前项（语音点歌与点击播都写入）。
"""


from core.utils.logger import logger

NAME = "cch_collect"

# 收藏写 Songloft 收藏歌单：可即时撤销的小副作用 → 复述即执行（分级语义见 guardrails）
RISK = "a"

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "收藏或取消收藏 CCH 屏正在播放的歌曲（写入用户的歌单）。"
            "用户说『收藏这首歌』『加入我的歌单』时 intent=true；"
            "『取消收藏』时 intent=false。只对 CCH 正在播放的歌有效；"
            "查询收藏列表请直说，本工具不做查询。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "boolean",
                    "description": "true=收藏，false=取消收藏；缺省 true",
                },
            },
            "required": [],
        },
    },
}


def _current_track_name() -> str:
    """播报文案用的当前曲名。复用 cch_np 的权威当前曲源（队列 entry item），
    不再自己读 _cch_state（消除重复读取逻辑）。"""
    from core.music import cch_np

    song = cch_np._current_from_cch_queue()
    return str((song or {}).get("name") or "")


async def run(args: dict) -> str:
    intent = args.get("intent")
    if not isinstance(intent, bool):
        intent = True
    name = _current_track_name()
    from core.music import cch_np

    result = await cch_np.collect_favorite(intent)
    action = str(result.get("action") or "")
    if action == "no_track":
        return "当前没有正在播放的 CCH 歌曲，先点一首歌再收藏"
    if action == "failed":
        detail = str(result.get("detail") or "")
        logger.warning(f"[tools/cch_collect] failed: {detail}")
        return f"收藏失败（歌单服务不可用：{detail}）"
    if intent:
        shown = f"《{name}》" if name else "当前歌曲"
        return f"已收藏{shown}" if action == "added" else f"{shown}已在你的收藏里"
    shown = f"《{name}》" if name else "当前歌曲"
    return f"已把{shown}从收藏移除"
