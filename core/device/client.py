"""类型化设备能力客户端。

业务代码只通过这里访问设备：每个方法对应能力表里的一条登记项，命令形状、
成功判据、回读方式都由 `registry` 声明，本模块只负责拼参数、发命令、判结果。

约定：
  - 任何调用都带超时；超时/异常返回明确错误（`error="timeout"`），不挂起。
  - 写操作返回「请求结果 + 回读结果」；读操作把解析值放在 `readback`。
  - 设备侧返回的 JSON 解析失败时 `request=None`，与旧 `run_shell` 的
    `None` 语义保持一致。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Awaitable, Callable

from core.device.registry import (
    CapabilitySpec,
    SuccessCriterion,
    ensure_script_allowed,
    resolve_capability,
)
from core.device.results import CapabilityResult, CommandResult
from core.device.transport import ShellTransport, XiaoAIShellTransport
from core.utils.base import json_decode, json_encode

# ---- 命令形状（能力层是唯一持有设备命令字面量的地方）----

MIC_GET = "[ ! -f /tmp/mipns/mute ] && echo on || echo off"
MIC_MUTE = 'ubus -t1 -S call pnshelper event_notify \'{"src":3, "event":7}\' 2>&1'
MIC_UNMUTE = 'ubus -t1 -S call pnshelper event_notify \'{"src":3, "event":8}\' 2>&1'
MIC_CLEAR_FLAG = "rm -f /tmp/mipns/mute"

PLAYER_GET_CONTEXT = "ubus call mediaplayer player_get_context"
PLAYBACK_STATUS = "mphelper mute_stat"
PLAYBACK_PAUSE = "mphelper pause"
PLAYBACK_RESUME = "mphelper play"
STOP_AUDIO = "killall tts_play.sh miplayer 2>/dev/null; mphelper pause"

SYSTEM_INFO = "ubus call system info"
DEVICE_INFO = "echo $(micocfg_model) $(micocfg_sn)"
BOOT_GET = "echo $(fw_env -g boot_part)"
BOOT_SET = "fw_env -s boot_part {partition} >/dev/null 2>&1 && echo $(fw_env -g boot_part)"

WAKE_SILENT = 'ubus call pnshelper event_notify \'{"src":1,"event":0}\''
WAKE_LOUD = 'ubus call pnshelper event_notify \'{"src":0,"event":0}\''
SLEEP = """
                ubus call pnshelper event_notify '{"src":3, "event":7}'
                sleep 0.1
                ubus call pnshelper event_notify '{"src":3, "event":8}'
            """

RESTART_XIAOAI = "/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1"

_BLOCKING_PLAY_TIMEOUT_MS = 600_000

# TTS 合成产物固定落在设备 /tmp/tts/ 下（见 docs/devices/lx06/tts-audio-direct-fetch.md）。
TTS_TEMP_PREFIX = "/tmp/tts/"


def _shell_quote(value: str) -> str:
    """shell 单引号转义（与旧 tts_play.sh 调用保持一致）。"""

    return "'" + str(value).replace("'", "'\\''") + "'"


def _json_string_body(value: str) -> str:
    """把文本转义成可安全嵌入 JSON 字符串字面量的内容（不含外层引号）。

    `tts_play.sh` 会把 `$1` 直接插进 `{"text":"$1"}`，所以回退路径必须先做
    JSON 转义、再做 shell 转义；顺序反了会被 shell 层破坏。
    """

    text = str(value)
    encoded = json_encode(text)
    if isinstance(encoded, str) and len(encoded) >= 2:
        return encoded[1:-1]
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _json_payload(data: dict[str, Any]) -> str:
    return json_encode(data) or "{}"


def _json_obj(text: str) -> dict[str, Any] | None:
    data = json_decode(text)
    return data if isinstance(data, dict) else None


def _clamp_volume(level: int) -> int:
    """百分比音量钳位到设备允许的 0-100（原厂 volume_up/down 不做钳位）。"""

    try:
        value = int(level)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(100, value))


def _decode_info(request: CommandResult | None) -> dict[str, Any]:
    """player_get_context / text_to_speech 的 `info` 是二次编码的 JSON 字符串。"""

    if request is None:
        return {}
    data = _json_obj(request.stdout)
    if data is None:
        return {}
    info = data.get("info")
    if isinstance(info, str):
        return _json_obj(info) or {}
    if isinstance(info, dict):
        return info
    return {}


def _find_instance(node: Any) -> dict[str, Any] | None:
    """在 procd `service list` 返回里递归找带 running 的实例对象。"""

    if isinstance(node, dict):
        if isinstance(node.get("running"), bool):
            return node
        for value in node.values():
            found = _find_instance(value)
            if found is not None:
                return found
    return None


class DeviceCapabilities:
    """能力层的类型化入口（单例，经 `get_device()` 获取）。"""

    def __init__(self, transport: ShellTransport | None = None):
        self._transport = transport or XiaoAIShellTransport()

    # ---- 执行内核 ----

    async def _run(
        self, script: str, timeout_ms: float
    ) -> tuple[CommandResult | None, str | None]:
        """发一条命令；异常映射为旧 `run_shell` 的失败形状。"""

        try:
            raw = await self._transport.run_shell(script, timeout_ms)
        except asyncio.TimeoutError:
            return CommandResult("error", "unknown", -1), "timeout"
        except Exception as exc:  # noqa: BLE001 - 设备边界必须收敛为明确错误
            return CommandResult("error", "unknown", -1), f"device_error: {type(exc).__name__}"

        data = json_decode(raw) if isinstance(raw, str) else None
        if not isinstance(data, dict) or not data:
            return None, None
        return (
            CommandResult(
                str(data.get("stdout", "")),
                str(data.get("stderr", "")),
                int(data.get("exit_code", 0)),
            ),
            None,
        )

    async def _invoke(
        self,
        spec: CapabilitySpec,
        script: str,
        timeout_ms: float | None = None,
        readback_fn: Callable[[], Awaitable[CapabilityResult]] | None = None,
        readback_expect: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        ensure_script_allowed(script)
        request, run_error = await self._run(
            script, timeout_ms if timeout_ms is not None else spec.timeout_ms
        )

        readback: dict[str, Any] | None = None
        readback_ok: bool | None = None
        readback_error: str | None = None
        if readback_fn is not None:
            try:
                rb = await readback_fn()
                readback = rb.readback
                if readback_expect is not None:
                    readback_ok = readback is not None and all(
                        readback.get(key) == value
                        for key, value in readback_expect.items()
                    )
                else:
                    readback_ok = rb.readback_ok
                if not rb.ok and rb.error:
                    readback_error = rb.error
            except Exception as exc:  # noqa: BLE001 - 回读失败也要显式返回
                readback_error = f"readback_failed: {type(exc).__name__}"

        ok, error = self._judge(spec, request, readback_ok, run_error)
        if not ok and error is None:
            error = readback_error or "capability_failed"
        return CapabilityResult(
            capability=spec.name,
            ok=ok,
            request=request,
            readback=readback,
            readback_ok=readback_ok,
            error=error,
            exception=run_error,
        )

    @staticmethod
    def _judge(
        spec: CapabilitySpec,
        request: CommandResult | None,
        readback_ok: bool | None,
        run_error: str | None,
    ) -> tuple[bool, str | None]:
        if spec.success is SuccessCriterion.READBACK:
            # 写操作：返回码不构成失败判据，只看回读。
            if readback_ok is None:
                return False, run_error or "readback_missing"
            if not readback_ok:
                return False, "readback_mismatch"
            return True, None

        if run_error is not None:
            return False, run_error
        if request is None:
            return False, "invalid_response"

        if spec.success is SuccessCriterion.EXIT_CODE:
            if request.exit_code == 0:
                return True, None
            return False, "exit_code_nonzero"

        if spec.success is SuccessCriterion.STDOUT_CONTAINS:
            if spec.expectation and spec.expectation in request.stdout:
                return True, None
            return False, "expected_output_missing"

        data = _json_obj(request.stdout)
        if data is None:
            return False, "json_invalid"

        if spec.success is SuccessCriterion.JSON_CODE:
            if data.get("code") == 0:
                return True, None
            return False, "code_nonzero"

        if spec.success is SuccessCriterion.JSON_PARSED:
            return True, None

        if spec.success is SuccessCriterion.JSON_FIELD:
            node: Any = data
            for part in (spec.expectation or "").split("."):
                if not isinstance(node, dict) or part not in node:
                    return False, "json_field_missing"
                node = node[part]
            return True, None

        return False, "unsupported_criterion"

    # ---- 只读查询 ----

    async def mic_get(self) -> CapabilityResult:
        spec = resolve_capability("mic_get")
        res = await self._invoke(spec, MIC_GET)
        mic = "on" if "on" in res.stdout else "off"
        return replace(res, readback={"mic": mic}, readback_ok=(mic == "on"))

    async def player_context(self) -> CapabilityResult:
        spec = resolve_capability("player_context")
        res = await self._invoke(spec, PLAYER_GET_CONTEXT)
        info = _decode_info(res.request)
        readback = {
            "status": info.get("status"),
            "volume": info.get("volume"),
            "player_mode": info.get("player_mode"),
        }
        return replace(res, readback=readback, readback_ok=res.ok)

    async def volume_get(self) -> CapabilityResult:
        ctx = await self.player_context()
        volume = (ctx.readback or {}).get("volume")
        ok = ctx.ok and volume is not None
        return CapabilityResult(
            capability="volume_get",
            ok=ok,
            request=ctx.request,
            readback={"volume": volume},
            readback_ok=volume is not None,
            error=None if ok else (ctx.error or "volume_missing"),
        )

    async def playback_status(self) -> CapabilityResult:
        spec = resolve_capability("playback_status")
        res = await self._invoke(spec, PLAYBACK_STATUS)
        return replace(
            res,
            readback={"status": res.stdout.strip()},
            readback_ok=res.ok,
        )

    async def service_status(self, name: str = "mediaplayer") -> CapabilityResult:
        spec = resolve_capability("service_status")
        script = f"ubus call service list '{_json_payload({'name': name})}'"
        res = await self._invoke(spec, script)
        data = _json_obj(res.stdout) if res.request is not None else None
        if data is None:
            # 传输失败 / 响应不可解析 ⇒ 无法判断存活（返回 None 由上层处理），
            # 不能把「读不到」当成「已死」。
            return replace(
                res,
                ok=False,
                readback=None,
                readback_ok=False,
                error=res.error or "invalid_response",
            )
        instance = _find_instance(data)
        if instance is None:
            return replace(
                res,
                ok=False,
                readback={"name": name, "running": False, "pid": None},
                readback_ok=False,
                error=res.error or "service_missing",
            )
        pid = instance.get("pid")
        return replace(
            res,
            readback={"name": name, "running": bool(instance.get("running")), "pid": pid},
            readback_ok=True,
        )

    async def system_memory(self) -> CapabilityResult:
        spec = resolve_capability("system_memory")
        res = await self._invoke(spec, SYSTEM_INFO)
        data = _json_obj(res.stdout) or {}
        memory = data.get("memory") if isinstance(data, dict) else None
        ok = res.ok and isinstance(memory, dict) and "free" in memory
        readback = {
            "total": memory.get("total") if isinstance(memory, dict) else None,
            "free": memory.get("free") if isinstance(memory, dict) else None,
        }
        return replace(
            res,
            ok=ok,
            readback=readback,
            readback_ok=ok,
            error=None if ok else (res.error or "memory_missing"),
        )

    async def device_info(self) -> CapabilityResult:
        spec = resolve_capability("device_info")
        res = await self._invoke(spec, DEVICE_INFO)
        return replace(res, readback={"raw": res.stdout.strip()}, readback_ok=res.ok)

    async def boot_partition_get(self) -> CapabilityResult:
        spec = resolve_capability("boot_partition_get")
        res = await self._invoke(spec, BOOT_GET)
        value = res.stdout.strip()
        return replace(res, readback={"boot_part": value}, readback_ok=bool(value))

    # ---- 可回读的写操作 ----

    async def mic_set(self, muted: bool) -> CapabilityResult:
        """设备真值：muted=True ⇒ event:7（建 mute 文件），False ⇒ event:8。"""

        spec = resolve_capability("mic_set")
        script = MIC_MUTE if muted else MIC_UNMUTE
        return await self._invoke(
            spec,
            script,
            readback_fn=self.mic_get,
            readback_expect={"mic": "off" if muted else "on"},
        )

    async def mic_clear_mute_flag(self) -> CapabilityResult:
        spec = resolve_capability("mic_clear_mute_flag")
        return await self._invoke(
            spec,
            MIC_CLEAR_FLAG,
            readback_fn=self.mic_get,
            readback_expect={"mic": "on"},
        )

    async def volume_set(self, level: int) -> CapabilityResult:
        """百分比音量通道：`player_set_continuous_volume` 不走档位映射。"""

        spec = resolve_capability("volume_set")
        target = _clamp_volume(level)
        payload: dict[str, Any] = {"volume": target, "media": "mediaplayer"}
        script = (
            "ubus call mediaplayer player_set_continuous_volume "
            f"'{_json_payload(payload)}'"
        )
        return await self._invoke(
            spec,
            script,
            readback_fn=self.volume_get,
            readback_expect={"volume": target},
        )

    async def boot_partition_set(self, partition: str) -> CapabilityResult:
        spec = resolve_capability("boot_partition_set")
        return await self._invoke(
            spec,
            BOOT_SET.format(partition=partition),
            readback_fn=self.boot_partition_get,
            readback_expect={"boot_part": partition},
        )

    # ---- 播放 / TTS ----

    async def play_url(
        self, url: str, timeout_ms: float | None = None
    ) -> CapabilityResult:
        spec = resolve_capability("play_url")
        script = (
            "ubus call mediaplayer player_play_url "
            f"'{_json_payload({'url': url, 'type': 1})}'"
        )
        return await self._invoke(
            spec, script, timeout_ms=timeout_ms, readback_fn=self.player_context
        )

    async def play_url_blocking(
        self, url: str, timeout_ms: float = _BLOCKING_PLAY_TIMEOUT_MS
    ) -> CapabilityResult:
        spec = resolve_capability("play_url_blocking")
        return await self._invoke(spec, f"miplayer -f '{url}'", timeout_ms=timeout_ms)

    async def play_file(
        self, path: str, timeout_ms: float = _BLOCKING_PLAY_TIMEOUT_MS
    ) -> CapabilityResult:
        if not path:
            raise ValueError("path is required")
        spec = resolve_capability("play_file")
        return await self._invoke(spec, f"miplayer -f '{path}'", timeout_ms=timeout_ms)

    async def play_text_blocking(
        self, text: str, timeout_ms: float = _BLOCKING_PLAY_TIMEOUT_MS
    ) -> CapabilityResult:
        """兜底阻塞播报：先 JSON 转义、再 shell 转义（顺序不可反）。"""

        spec = resolve_capability("play_text_blocking")
        script = (
            f"/usr/sbin/tts_play.sh {_shell_quote(_json_string_body(text or '你好'))}"
        )
        return await self._invoke(spec, script, timeout_ms=timeout_ms)

    async def tts_speak(
        self, text: str, timeout_ms: float | None = None
    ) -> CapabilityResult:
        spec = resolve_capability("tts_speak")
        script = (
            "ubus call mibrain text_to_speech "
            f"'{_json_payload({'text': text or '你好', 'save': 0})}'"
        )
        return await self._invoke(spec, script, timeout_ms=timeout_ms)

    async def tts_synthesize(
        self, text: str, timeout_ms: float | None = None
    ) -> CapabilityResult:
        spec = resolve_capability("tts_synthesize")
        script = (
            "ubus call mibrain text_to_speech "
            f"'{_json_payload({'text': text, 'save': 1, 'play': 0})}'"
        )
        res = await self._invoke(spec, script, timeout_ms=timeout_ms)
        path = _decode_info(res.request).get("path")
        if not path:
            return replace(res, ok=False, error="path_missing")

        exists = (await self.tts_file_exists(str(path))).ok
        ok = res.ok and exists
        return replace(
            res,
            ok=ok,
            readback={"path": path, "exists": exists},
            readback_ok=exists,
            error=None if ok else (res.error or "readback_mismatch"),
        )

    async def tts_file_exists(self, path: str) -> CapabilityResult:
        spec = resolve_capability("tts_file_exists")
        res = await self._invoke(
            spec, f"[ -f {_shell_quote(path)} ] && echo yes || echo no"
        )
        exists = "yes" in res.stdout
        return replace(res, readback={"exists": exists}, readback_ok=exists)

    async def tts_cleanup(self, path: str) -> CapabilityResult:
        """删除合成临时文件；前缀不是 `/tmp/tts/` 一律拒绝（防误删）。"""

        if not path or not path.startswith(TTS_TEMP_PREFIX):
            raise ValueError(f"refusing to remove non-tts temp file: {path!r}")
        spec = resolve_capability("tts_cleanup")
        return await self._invoke(spec, f"rm -f {_shell_quote(path)}")

    # ---- 播放 / 服务控制 ----

    async def pause(self, timeout_ms: float | None = None) -> CapabilityResult:
        spec = resolve_capability("pause")
        return await self._invoke(
            spec, PLAYBACK_PAUSE, timeout_ms=timeout_ms, readback_fn=self.player_context
        )

    async def playback_resume(self) -> CapabilityResult:
        spec = resolve_capability("playback_resume")
        return await self._invoke(spec, PLAYBACK_RESUME)

    async def stop_audio(self) -> CapabilityResult:
        spec = resolve_capability("stop_audio")
        return await self._invoke(spec, STOP_AUDIO)

    async def wake(self, silent: bool = True) -> CapabilityResult:
        spec = resolve_capability("wake")
        return await self._invoke(spec, WAKE_SILENT if silent else WAKE_LOUD)

    async def sleep(self) -> CapabilityResult:
        spec = resolve_capability("sleep")
        return await self._invoke(spec, SLEEP)

    async def ask_xiaoai(self, text: str, silent: bool = False) -> CapabilityResult:
        spec = resolve_capability("ask_xiaoai")
        data: dict[str, Any] = {"nlp": 1, "nlp_text": text}
        if not silent:
            data["tts"] = 1
        return await self._invoke(
            spec, f"ubus call mibrain ai_service '{_json_payload(data)}'"
        )

    async def restart_xiaoai_service(self) -> CapabilityResult:
        spec = resolve_capability("restart_xiaoai_service")
        return await self._invoke(spec, RESTART_XIAOAI)

    # ---- 通用入口 ----

    async def shell(self, script: str, timeout_ms: float = 10000) -> CapabilityResult:
        spec = resolve_capability("shell")
        return await self._invoke(spec, script, timeout_ms=timeout_ms)

    async def execute(self, name: str, **params: Any) -> CapabilityResult:
        """按名字调度能力（禁区与未登记都拒绝）。"""

        spec = resolve_capability(name)
        _validate_params(spec, params)
        handler = getattr(self, name)
        return await handler(**params)


def _validate_params(spec: CapabilitySpec, params: dict[str, Any]) -> None:
    allowed = {p.name for p in spec.params}
    unknown = set(params) - allowed
    if unknown:
        raise TypeError(f"{spec.name} 不接受参数：{sorted(unknown)}")

    for param in spec.params:
        if param.required and param.name not in params:
            raise TypeError(f"{spec.name} 缺少参数：{param.name}")
        value = params.get(param.name)
        if value is None:
            continue
        if param.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{spec.name}.{param.name} 需要 int")
        elif param.type == "bool":
            if not isinstance(value, bool):
                raise TypeError(f"{spec.name}.{param.name} 需要 bool")
        elif param.type == "str":
            if not isinstance(value, str):
                raise TypeError(f"{spec.name}.{param.name} 需要 str")


_DEFAULT: DeviceCapabilities | None = None


def get_device() -> DeviceCapabilities:
    """取全局能力层单例（传输层在调用时才解析 XiaoAI，测试可随时替换）。"""

    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = DeviceCapabilities()
    return _DEFAULT


def set_device(capabilities: DeviceCapabilities) -> None:
    global _DEFAULT
    _DEFAULT = capabilities


def reset_device() -> None:
    """测试辅助：丢弃单例。"""

    global _DEFAULT
    _DEFAULT = None
