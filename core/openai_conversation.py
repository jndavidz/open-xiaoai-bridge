"""OpenAI-compatible continuous conversation controller."""

from core.openai import OpenAIManager
from core.external_conversation import ExternalConversationController
from core.utils import turn_trace
from core.utils.logger import logger
from core.utils.new_topic import match_new_topic


class OpenAIConversationController(ExternalConversationController):
    """Manages multi-turn conversation for OpenAI-compatible services."""

    CONFIG_PREFIX = "openai"
    BACKEND_NAME = "OpenAI"
    LOG_MODULE = "OpenAI Conv"
    WAKEUP_SOURCE = "openai"
    MANAGER = OpenAIManager

    async def _maybe_handle_new_topic(self, text: str) -> bool:
        """「换个话题」暗号（spec topic-switch-and-memory 票 03，方案 B）。

        命中即**同一临界区原子执行**：清 bridge 本地历史（reset_session）+
        置下轮 X-Session-Action: new 信令（force_new_session）——半态（bridge
        已当新话题、aurora 还在旧会话）不允许存在。随后直接 TTS 过渡语，
        不发 LLM（省 2–4s 模型延迟）；不退出对话循环。
        """
        cfg = self.config.get_app_config("openai.new_topic", None)
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return False
        if not match_new_topic(
            text,
            exact=list(cfg.get("exact", []) or []),
            fuzzy=list(cfg.get("fuzzy", []) or []),
        ):
            return False
        logger.info("New-topic passphrase matched - resetting session", module=self.LOG_MODULE)
        turn_trace.abandon_turn()  # 过渡轮没有 LLM/TTS，立即收尾为 incomplete
        self.backend.reset_session()
        self.backend.force_new_session()
        transition = str(cfg.get("transition", "") or "")
        if transition:
            await self._play_bypass_text(transition, reason="new topic transition")
        return True
