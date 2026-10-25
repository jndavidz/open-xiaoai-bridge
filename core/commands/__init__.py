"""指令层深模块（C4/P2 ticket 01）：双指令表执行器 + HA client + 动作构造器 + 唤醒边界路由。

config（数据面，热重载）只留两张指令表、KWS 词表、人设表与路由钩子绑定；
本包（逻辑面，随镜像）持有全部执行逻辑，config 不再反向 import 核心单例。

对外接口（config 钩子只用这几个名字）：
  - route_before_wakeup(...)  唤醒边界默认路由（表命中执行 / 停止聆听 / 人设切换 / 截胡清场）
  - run_steps(speaker, steps) 步骤序列执行器（str=播报 / ("ha",…)=HA / callable）
  - ha_script / ha_switch     声明式动作构造器（config 数据行用）
  - kodi_launch(speaker)      特殊动作：ADB 启动电视端 KODI
"""

from core.commands.actions import ha_script, ha_switch, kodi_launch
from core.commands.executor import run_steps
from core.commands.hass import hass_action
from core.commands.router import route_before_wakeup, stop_listening

__all__ = [
    "hass_action",
    "ha_script",
    "ha_switch",
    "kodi_launch",
    "run_steps",
    "route_before_wakeup",
    "stop_listening",
]
