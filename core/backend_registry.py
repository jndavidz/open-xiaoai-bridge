"""外部后端注册表（P0 票 07，C1 contract 步）。

「一个后端」在代码里只登记一次：条目引用管理器子类与控制器壳。
配置前缀与启用环境变量直接取自管理器类属性（票 04 引入的身份字段），
本表不复制第二份事实源。

- 新增后端 = 加一个 BackendManagerBase 子类 + 在 BACKENDS 加一条登记。
- xiaozhi 是双向音频流范式，保持注册表外特殊条目（既定裁决，不强求统一）。
- 控制器壳以点路径惰性解析：external_conversation 有导入期音频解码副作用，
  不能提前到 wakeup_session 的模块导入链上（api_server/测试经彼导入）。
"""

import importlib
from dataclasses import dataclass

from core.openclaw import OpenClawManager
from core.openai import OpenAIManager
from core.qwenpaw import QwenPawManager

# 输入模式：连续对话（唤醒后进入 VAD/ASR → 后端 → TTS 循环，经控制器壳驱动）
INPUT_CONVERSATION = "conversation"


@dataclass(frozen=True)
class BackendEntry:
    """注册表条目：一个外部后端的唯一登记处。"""

    # before_wakeup 返回的后端名字符串（如 "openclaw"），语义不变
    name: str
    # BackendManagerBase 子类（classmethod 单例风格）
    manager: type
    # 控制器壳（ExternalConversationController 子类），"模块:属性" 惰性点路径
    controller_path: str
    # 输入模式（本批全部为连续对话）
    input_mode: str = INPUT_CONVERSATION

    @property
    def config_prefix(self) -> str:
        return self.manager.CONFIG_PREFIX

    @property
    def enable_env_vars(self) -> tuple[str, ...]:
        return self.manager.ENABLE_ENV_VARS

    @property
    def display_name(self) -> str:
        return self.manager.LOG_MODULE

    def load_controller(self) -> type:
        """解析并返回控制器壳类（首次调用时导入，此后走 sys.modules 缓存）。"""
        module_name, _, attr = self.controller_path.partition(":")
        module = importlib.import_module(module_name)
        return getattr(module, attr)


BACKENDS: dict[str, BackendEntry] = {
    entry.name: entry
    for entry in (
        BackendEntry(
            name="openclaw",
            manager=OpenClawManager,
            controller_path="core.openclaw_conversation:OpenClawConversationController",
        ),
        BackendEntry(
            name="openai",
            manager=OpenAIManager,
            controller_path="core.openai_conversation:OpenAIConversationController",
        ),
        BackendEntry(
            name="qwenpaw",
            manager=QwenPawManager,
            controller_path="core.qwenpaw_conversation:QwenPawConversationController",
        ),
    )
}


class BackendRegistry:
    """注册表的查表入口（公开接口；消费方不得直写管理器私有属性）。"""

    def get(self, name: str) -> BackendEntry | None:
        return BACKENDS.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(BACKENDS)

    def reset_session_keys(self, config_manager=None):
        """把全部后端会话键复位到当前配置默认值（每次唤醒前调用）。

        复位语义在管理器侧（BackendManagerBase.reset_session_key），
        注册表只负责遍历公开接口。
        """
        for entry in BACKENDS.values():
            entry.manager.reset_session_key(config_manager)


backend_registry = BackendRegistry()
