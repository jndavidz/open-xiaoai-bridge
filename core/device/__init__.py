"""设备能力层：业务代码访问音箱的唯一入口。

用法：

    from core.device import get_device

    caps = get_device()
    result = await caps.mic_get()
    result = await caps.volume_set(35)   # result.ok / result.readback

新增设备能力 = 在 `registry.CAPABILITIES` 加一条登记 + 在
`client.DeviceCapabilities` 加一个具名方法，不改会话逻辑。
禁区方法在 `registry.FORBIDDEN_METHODS` 登记，不进入能力表。
"""

from core.device.client import (
    TTS_TEMP_PREFIX,
    DeviceCapabilities,
    get_device,
    reset_device,
    set_device,
)
from core.device.registry import (
    CAPABILITIES,
    FORBIDDEN_METHODS,
    CapabilityGroup,
    CapabilitySpec,
    DeviceCapabilityError,
    ForbiddenCapabilityError,
    ParamSpec,
    SuccessCriterion,
    UnknownCapabilityError,
    capability_table,
    ensure_script_allowed,
    resolve_capability,
)
from core.device.results import CapabilityResult, CommandResult
from core.device.transport import ShellTransport, XiaoAIShellTransport

__all__ = [
    "CAPABILITIES",
    "FORBIDDEN_METHODS",
    "CapabilityGroup",
    "CapabilityResult",
    "CapabilitySpec",
    "CommandResult",
    "DeviceCapabilities",
    "DeviceCapabilityError",
    "ForbiddenCapabilityError",
    "ParamSpec",
    "ShellTransport",
    "SuccessCriterion",
    "TTS_TEMP_PREFIX",
    "UnknownCapabilityError",
    "XiaoAIShellTransport",
    "capability_table",
    "ensure_script_allowed",
    "get_device",
    "reset_device",
    "resolve_capability",
    "set_device",
]
