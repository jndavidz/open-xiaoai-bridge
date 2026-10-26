"""LED 灯语（A-6/P3 票 02）：管线态四态可视化。

看灯就知道音箱的状态：

    聆听（listening）  = 全环蓝（静态定格）
    思考（processing） = 全环琥珀（原厂黄 0xF0A902）
    播报（speaking）   = 不干预——播放自动带动原厂 L=3/L=12 动画
                        （player 自己用；sysfs 直写会与 ledserver 抢占）
    空闲（idle）       = 全环写黑归位
    打断（turn_ended outcome=cancelled）= 红色快闪一次
    故障（outcome=error）= 红色定格数秒后归位

实现：订阅 `core.utils.pipeline` 的状态事件（subscribe_state，线程上抛、
快进快出），回调内只做「投递到主循环」——设备写全部走 asyncio 任务，经
`LedLanguage` 内的串行锁逐条下发（ledserver 内部队列会互相抢占，事实层
led-control-api.md §6：串行调用）。

纪律（登记在能力表 notes）：sysfs 与 led show 互抢占，同一时段只用一条；
bridge 只用 sysfs 直写（静态色），不调 led show 动画（那是系统的）；
任何效果用完必须写黑归位；异常排障先断电（led-control-api.md §5.2）。

开关：`led_language.enabled`（默认关）——真机看灯验收后转正；面板热重载
即时生效。夜间降亮度（Fog）留真机窗口定，当前仅参数化占位。
"""

from __future__ import annotations

import asyncio

from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.utils import pipeline

LOG_MODULE = "LedLanguage"

# AW20054 共 18 灯（index 0-17，事实层 led-control-api.md §1）
PIXEL_COUNT = 18

# 配色（原厂色板风格，led-control-api.md §5.1：绿=就绪 红=告警 黄/琥珀 青灰）
COLOR_LISTENING = "2e6fdb"  # 蓝自选（原厂表无蓝）
COLOR_PROCESSING = "f0a902"  # 原厂琥珀
COLOR_ERROR = "f85869"  # 原厂红
COLOR_OFF = "000000"

# 打断红闪 / 故障定格时长（秒；真机验收可调）
INTERRUPT_FLASH_SECONDS = 0.3
ERROR_HOLD_SECONDS = 4.0


class LedLanguage:
    """管线态 → LED 灯语（订阅制；attach 后按 config 开关生效）。"""

    def __init__(self):
        self.config = ConfigManager.instance()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe = None
        self._serial = asyncio.Lock()
        # 正在持有的灯色（None=未点灯）：归位只清自己点过的灯
        self._held_color: str | None = None

    # ---- 生命周期 ----

    def enabled(self) -> bool:
        return bool(self.config.get_app_config("led_language.enabled", False))

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """订阅管线事件（幂等；灯语任务投递到指定事件循环）。"""
        if self._unsubscribe is not None:
            return
        self._loop = loop
        self._unsubscribe = pipeline.subscribe_state(self._on_pipeline_event)
        logger.info(f"attached (enabled={self.enabled()})", module=LOG_MODULE)

    def detach(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # ---- 管线事件回调（触发线程上抛：只投递，不碰设备） ----

    def _on_pipeline_event(self, event: str, data: dict) -> None:
        if not self.enabled() or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._render(event, data), self._loop
            )
        except Exception as exc:  # noqa: BLE001 - 灯语绝不影响对话主流程
            logger.warning(
                f"schedule failed: {type(exc).__name__}: {exc}", module=LOG_MODULE
            )

    async def _render(self, event: str, data: dict) -> None:
        """把管线事件翻译成灯语（全部经串行锁）。"""
        try:
            if event == "state_changed":
                await self._render_state(data.get("state"))
            elif event == "turn_ended":
                outcome = str(data.get("outcome") or "")
                if outcome.startswith("cancelled"):
                    await self._flash(COLOR_ERROR, INTERRUPT_FLASH_SECONDS)
                elif outcome == "error":
                    await self._flash(COLOR_ERROR, ERROR_HOLD_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"render failed: {type(exc).__name__}: {exc}", module=LOG_MODULE
            )

    async def _render_state(self, state: str | None) -> None:
        from core.device import get_device

        device = get_device()
        if state == "listening":
            await self._fill(device, COLOR_LISTENING)
        elif state == "processing":
            await self._fill(device, COLOR_PROCESSING)
        elif state == "speaking":
            # 播放自动带动原厂动画（L=3/L=12）：不干预、不清灯（系统收尾）
            logger.debug("speaking: system animation owns the LEDs", module=LOG_MODULE)
        elif state == "idle":
            await self._clear(device)

    # ---- 灯面原语（串行锁内执行） ----

    async def _write_all(self, device, color: str) -> None:
        """单条 shell 批量写 18 灯（真机实测：逐灯 RPC 每条一次设备往返，
        18 条 ≈1s+，琥珀会迟到；合并为一条往返，观感即时）。"""
        script = (
            "for i in $(seq 0 {n}); do echo \"$i 0x{c}\" > "
            "/sys/devices/i2c-0/0-003a/led_rgb; done"
        ).format(n=PIXEL_COUNT - 1, c=color)
        await device.shell(script)

    async def _fill(self, device, color: str) -> None:
        async with self._serial:
            await self._write_all(device, color)
            self._held_color = color

    async def _clear(self, device) -> None:
        if self._held_color is None:
            return  # 没点过灯就不碰（不抢系统的灯）
        async with self._serial:
            await self._write_all(device, COLOR_OFF)
            self._held_color = None

    async def _flash(self, color: str, seconds: float) -> None:
        """点全环色、停留 seconds、写黑归位（打断红闪/故障定格共用）。

        停留期间**持锁**：打断后新监听轮的蓝环在同一循环排队，若锁外
        sleep，蓝环会在红闪停留期间插队覆盖（真机实测红不可见）。
        """
        from core.device import get_device

        device = get_device()
        async with self._serial:
            await self._write_all(device, color)
            self._held_color = color
            await asyncio.sleep(seconds)
            await self._write_all(device, COLOR_OFF)
            self._held_color = None


_DEFAULT: LedLanguage | None = None


def get_led_language() -> LedLanguage:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = LedLanguage()
    return _DEFAULT


def reset_led_language() -> None:
    """测试辅助：丢弃单例（含订阅注销）。"""
    global _DEFAULT
    if _DEFAULT is not None:
        _DEFAULT.detach()
    _DEFAULT = None
