"""指令动作构造器（声明式：一行声明一条指令）+ 特殊动作。

表数据写在 config（数据面，热重载），动作语义在 core（逻辑面，随镜像）——
构造器是两者的接点：config 用它们把数据行编译成执行器认识的步骤元组。
"""

from core.commands import hass


def ha_script(name: str):
    """一行声明调用一个 HA script（如 hifi_mode / music_stop / music_next）。"""
    return ("ha", "script", "turn_on", {"entity_id": f"script.{name}"})


def ha_switch(entity_id: str):
    """一行声明打开一个 HA switch（如 WOL 网络唤醒开关 switch.nuc_hifi_wol）。"""
    return ("ha", "switch", "turn_on", {"entity_id": entity_id})


# ---------- 网络层原子控制：KODI 启动（经 HA androidtv 集成走 ADB） ----------
# 前提（缺一不可，均不属本模块职责）：
#   ① Vidda 电视已开机——Vidda 无 WOL，关机即网络全断，ADB 不可达；开机属红外/Phase E；
#   ② 电视已安装 KODI（org.xbmc.kodi）——2026-08-29 联机实测已确认安装，主 Activity=org.xbmc.kodi/.Splash
#      （Splash 为 LAUNCHER 入口，启动后接管到 .Main；.StartActivity/.Main 均非 LAUNCHER，勿用）。
# 满足前提后，HA androidtv 集成自动 ADB 重连，本调用即时生效；否则调用失败（仅记日志、不崩溃）。
async def kodi_launch(speaker):
    """经 HA androidtv 集成向电视发 ADB 指令启动 KODI（STRM 直链播放入口）。"""
    await hass.hass_action(
        "androidtv", "adb_command",
        {"entity_id": "media_player.vidda_tv",
         "command": "am start -n org.xbmc.kodi/.Splash"},
    )
