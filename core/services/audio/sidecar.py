"""8ch 旁路采集器（lx06-audio-sidecar spec 票 01）。

为什么存在：client 的采集命令硬编码 ``-c 6``（只推六麦），谱减所需的播放
回环参考（ch6/ch7，见 ws: docs/devices/lx06/mic-array-6ch-capture.md §11）
根本不在主链里。设备的 ALSA 设备 ``noop`` 是 ``dsnoop``（共享），外部
``arecord -c 8`` 与 client 的 6ch 采集**可以同时跑**（同文 §3.2 实测）。
本模块把这条路接进 bridge：通过可配置命令（生产 = SSH 到设备执行 8ch
arecord）把原始交错字节流读回，按 8ch 帧对齐后交给唤醒链（票 02）。

设计约束（spec Implementation Decisions，违反即返工）：

1. **近端与参考必须同源**：本模块只产出一条 8ch 流，ch0–5 与 ch6 来自
   同一次 ALSA 读取。把这里的 ch6 拿去和 client 的 6ch 拼接是被 spec
   明确禁止的跨流对齐——消费侧（wakeup_input）有断言守住。
2. **只读设备**：远程命令只写 ``/tmp``（tmpfs）PID 文件，不碰 ``/data``。
3. **不与 client 互斥**：dsnoop 并发；本模块对 client 无任何操作。
4. **失败即回落**：本模块不抛错、不阻塞主环；断流退避重连，健康状态由
   :meth:`SidecarCaptureRunner.is_healthy` 供消费侧轮询。
5. **进程树干净**：远程命令先落 PID 文件再 ``exec arecord``（exec 不换
   PID）；启动前与每次退出都执行 cleanup 命令按 PID 回收——判据是
   ``/proc/<pid>/cmdline`` 含 ``arecord``（防 PID 复用误杀、也绝不会碰
   client 自己的 arecord）。

测试边界（spec Testing Decisions）：命令构造、分帧对齐、退避序列、
清理调用全部可离线断言；SSH 细节不在测试范围——单测用假采集子进程
（一个往 stdout 写字节的本地 Python 进程）驱动真实 asyncio 管道。
"""

from __future__ import annotations

import asyncio
import shlex
import time
from collections import deque
from typing import Callable

from core.services.audio.pcm import to_int16
from core.utils.logger import logger

# LX06 侧车默认采集规格（spec：16 kHz / S32_LE / 交错原始流）
SIDECAR_CHANNELS = 8
SIDECAR_RATE = 16000
SIDECAR_BITS = 32
# /tmp 为 tmpfs（硬约束 2：只写 /tmp 不写 /data）
DEFAULT_PID_FILE = "/tmp/oxai-sidecar-arecord.pid"
# 健康判据窗口（秒）：一段时间无任何字节即视为断流。client 读循环 500ms
# 无数据即退出；dsnoop 下 arecord 正常产帧间隔远小于该值。
DEFAULT_STALL_TIMEOUT = 2.0
DEFAULT_BACKOFF_INITIAL = 1.0
DEFAULT_BACKOFF_MAX = 30.0
# 连续健康运行超过该秒数后，退避步长回到初值（长期稳定连接不应累积长退避）
HEALTHY_RESET_WINDOW = 60.0
# 读块大小：8ch/S32/16k ≈ 512 KB/s，4 KB ≈ 8 ms 粒度，兼顾延迟与 syscall 开销
READ_CHUNK = 4096
# 分帧缓冲上限（字节）：超过即丢弃并计数（消费方卡死时不无限膨胀）
MAX_FRAME_BUFFER = 1 << 20

# 老固件 dropbear 需要 ssh-rsa 协商（deploy/ubus-sidecar.py 同款实测参数）
DEFAULT_SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=3",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
)


def build_arecord_command(
    host: str,
    key_path: str,
    *,
    user: str = "root",
    port: int = 22,
    device: str = "noop",
    channels: int = SIDECAR_CHANNELS,
    rate: int = SIDECAR_RATE,
    bits_per_sample: int = SIDECAR_BITS,
    pid_file: str = DEFAULT_PID_FILE,
    ssh_opts: tuple | list = DEFAULT_SSH_OPTS,
) -> list[str]:
    """构造 SSH 采集命令（纯函数，可单测）。

    远端命令先把自己的 shell PID 写进 ``pid_file`` 再 ``exec arecord``：
    ``exec`` 不换 PID，所以 PID 文件里就是 arecord 的 PID，cleanup 命令
    据此精确回收（见 build_cleanup_command）。
    """
    remote = (
        f"echo $$ > {pid_file} && "
        f"exec arecord -D {device} -f S{int(bits_per_sample)}_LE "
        f"-r {int(rate)} -c {int(channels)} -t raw -"
    )
    return [
        "ssh", *ssh_opts,
        "-p", str(int(port)),
        "-i", key_path,
        f"{user}@{host}",
        remote,
    ]


def build_cleanup_command(
    host: str,
    key_path: str,
    *,
    user: str = "root",
    port: int = 22,
    pid_file: str = DEFAULT_PID_FILE,
    ssh_opts: tuple | list = DEFAULT_SSH_OPTS,
) -> list[str]:
    """构造残留进程回收命令（纯函数，可单测）。

    只 kill「PID 文件里登记且 cmdline 含 arecord」的进程——PID 复用时
    不误杀无关进程，client 自己的 6ch arecord（不同 PID）永不被触碰。
    """
    remote = (
        f'p=$(cat {pid_file} 2>/dev/null); '
        f'if [ -n "$p" ] && grep -q arecord /proc/$p/cmdline 2>/dev/null; '
        f'then kill "$p" 2>/dev/null; fi'
    )
    return ["ssh", *ssh_opts, "-p", str(int(port)), "-i", key_path,
            f"{user}@{host}", remote]


def parse_sidecar_config(section: dict | None) -> dict | None:
    """解析 ``audio_input.sidecar``；不可用（未启用/参数不完整）返回 None。

    ``cmd`` 非空时按 shell 词法切分、原样作为采集命令（部署侧自定拓扑，
    例如 NAS 侧 pump 经 TCP 投喂）；否则按 host/key_path 经
    build_arecord_command 构造。``ssh_opts`` 追加 ssh 参数（替换默认组）——
    生产容器用它注入 StrictHostKeyChecking/UserKnownHostsFile（与
    event_plane.native_cmd 同款密钥/known_hosts 挂载）。
    """
    section = section if isinstance(section, dict) else {}
    if not section.get("enabled"):
        return None
    raw_opts = section.get("ssh_opts")
    ssh_opts = (
        tuple(str(o) for o in raw_opts) if isinstance(raw_opts, (list, tuple))
        else DEFAULT_SSH_OPTS
    )
    try:
        channels = int(section.get("channels", SIDECAR_CHANNELS))
        rate = int(section.get("rate", SIDECAR_RATE))
        bits = int(section.get("bits_per_sample", SIDECAR_BITS))
        port = int(section.get("port", 22))
        stall = float(section.get("stall_timeout", DEFAULT_STALL_TIMEOUT))
        backoff_initial = float(section.get("backoff_initial", DEFAULT_BACKOFF_INITIAL))
        backoff_max = float(section.get("backoff_max", DEFAULT_BACKOFF_MAX))
    except (TypeError, ValueError):
        logger.warning("[Sidecar] 数值型配置非法，sidecar 不可用", module="XiaoAI")
        return None
    channels = max(2, min(channels, 8))
    bits = 32 if bits == 32 else 16

    cmd_raw = section.get("cmd")
    cmd_override = str(cmd_raw).strip() if cmd_raw else ""
    host = str(section.get("host", "") or "").strip()
    key_path = str(section.get("key_path", "") or "").strip()
    if cmd_override:
        command = shlex.split(cmd_override)
    elif host and key_path:
        command = build_arecord_command(
            host, key_path,
            user=str(section.get("user", "root")),
            port=port, channels=channels, rate=rate, bits_per_sample=bits,
            pid_file=str(section.get("pid_file", DEFAULT_PID_FILE)),
            ssh_opts=ssh_opts,
        )
    else:
        logger.warning(
            "[Sidecar] 已启用但缺 host/key_path（或 cmd），sidecar 不可用",
            module="XiaoAI",
        )
        return None

    cleanup_command = None
    if not cmd_override and host and key_path:
        cleanup_command = build_cleanup_command(
            host, key_path,
            user=str(section.get("user", "root")),
            port=port,
            pid_file=str(section.get("pid_file", DEFAULT_PID_FILE)),
            ssh_opts=ssh_opts,
        )
    return {
        "command": command,
        "cleanup_command": cleanup_command,
        "channels": channels,
        "rate": rate,
        "bits": bits,
        "stall_timeout": max(0.3, min(stall, 30.0)),
        "backoff_initial": max(0.1, min(backoff_initial, 60.0)),
        "backoff_max": max(backoff_initial, min(backoff_max, 600.0)),
    }


class SidecarFramer:
    """字节流 → 整帧切分（保留残留，绝不丢数据）。

    产出恒为 ``frame_bytes`` 整数倍。块间残留属正常流水线状态（下一块补齐），
    不计错误；只有真实丢字节（缓冲超限丢弃，``dropped_bytes``）才计数。
    """

    def __init__(self, frame_bytes: int) -> None:
        self.frame_bytes = max(1, int(frame_bytes))
        self._buf = bytearray()
        self.counters = {"dropped_bytes": 0}

    def feed(self, chunk: bytes) -> bytes:
        if chunk:
            self._buf.extend(chunk)
        if len(self._buf) > MAX_FRAME_BUFFER:
            self.counters["dropped_bytes"] += len(self._buf)
            self._buf.clear()
            return b""
        n = len(self._buf) - len(self._buf) % self.frame_bytes
        if n <= 0:
            return b""
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def reset(self) -> None:
        self._buf.clear()


class SidecarCaptureRunner:
    """采集子进程的生命周期管理：拉起 → 泵流 → 断流检测 → 退避重连。

    ``command`` 是完整 argv（生产 = ssh …；测试 = 假采集子进程）。不抛错、
    不阻塞：run() 内部消化一切异常，健康状态经 stats()/is_healthy() 暴露。
    线程口径：run()/stats() 跑在事件 loop 线程，stop()/is_healthy() 可从
    其他线程调（flag 读写在 GIL 下安全）；计数器为尽力而为的观测值
    （跨线程丢增量可接受，不作控制流判据——控制流只看 is_healthy）。
    """

    def __init__(
        self,
        command: list[str],
        *,
        cleanup_command: list[str] | None = None,
        channels: int = SIDECAR_CHANNELS,
        bits: int = SIDECAR_BITS,
        stall_timeout: float = DEFAULT_STALL_TIMEOUT,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        on_frame: Callable[[bytes], None] | None = None,
        name: str = "sidecar",
        config_sig: tuple | None = None,
    ) -> None:
        self.command = list(command)
        self.cleanup_command = list(cleanup_command) if cleanup_command else None
        self.channels = int(channels)
        self.bits = int(bits)
        self.stall_timeout = float(stall_timeout)
        self.backoff_initial = float(backoff_initial)
        self.backoff_max = float(backoff_max)
        self.on_frame = on_frame
        self.name = name
        self.config_sig = config_sig

        self.frame_bytes = self.channels * 2  # S16 交错
        self._framer = SidecarFramer(self.frame_bytes)
        self._running = False
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._last_data_at = 0.0
        self._started_at = 0.0
        self._rate_window: deque[tuple[float, int]] = deque()
        self.counters = {
            "frames": 0,
            "align_errors": 0,
            "spawn_failures": 0,
            "stalls": 0,
            "reconnects": 0,
            "callback_errors": 0,
            "cleanup_runs": 0,
        }
        self.last_error: str | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        """同步可调（配置热关、停机路径，可能来自非 loop 线程）。"""
        self._running = False
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # loop 已关闭
                pass

    @property
    def running(self) -> bool:
        return self._running

    async def run(self) -> None:
        """重启主循环：拉起 → 泵流 → （断流/EOF/异常）→ 退避 → 重拉。"""
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._running = True
        backoff = self.backoff_initial
        while self._running:
            self._started_at = time.monotonic()
            got_data = False
            proc = None
            try:
                proc = await self._spawn()
            except Exception as exc:  # noqa: BLE001
                self.counters["spawn_failures"] += 1
                self.last_error = f"spawn: {type(exc).__name__}: {exc}"
                logger.warning(
                    f"[Sidecar:{self.name}] 拉起失败: {exc}", module="XiaoAI"
                )
            if proc is not None:
                try:
                    got_data = await self._pump(proc)
                except asyncio.TimeoutError:
                    self.counters["stalls"] += 1
                    self.last_error = "stall"
                    logger.warning(
                        f"[Sidecar:{self.name}] 断流（{self.stall_timeout:.1f}s 无字节），"
                        "回收后重连",
                        module="XiaoAI",
                    )
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"pump: {type(exc).__name__}: {exc}"
                    logger.warning(
                        f"[Sidecar:{self.name}] 泵流异常: {exc}", module="XiaoAI"
                    )
                finally:
                    if proc is not None:
                        await self._teardown(proc)
            if not self._running:
                break
            self.counters["reconnects"] += 1
            # 长期稳定连接退避归零；短命会话才指数增长
            if got_data and time.monotonic() - self._started_at >= HEALTHY_RESET_WINDOW:
                backoff = self.backoff_initial
            else:
                backoff = min(backoff * 2, self.backoff_max)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                break  # stop() 置位：立即退出，不再重连
            except asyncio.TimeoutError:
                pass

    async def _spawn(self) -> asyncio.subprocess.Process:
        # 先回收上一轮残留（容器重启后 ssh 死了远端 arecord 可能还活着）
        await self._run_cleanup()
        return await asyncio.create_subprocess_exec(
            *self.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _run_cleanup(self) -> None:
        if not self.cleanup_command:
            return
        self.counters["cleanup_runs"] += 1
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.cleanup_command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception as exc:  # noqa: BLE001 - 清理失败只告警，不阻塞采集
            logger.warning(
                f"[Sidecar:{self.name}] 清理命令失败: "
                f"{type(exc).__name__}: {exc or '(无详情，常见为超时)'}",
                module="XiaoAI",
            )

    async def _teardown(self, proc: asyncio.subprocess.Process) -> None:
        """停本地子进程 + 显式回收远端 arecord（不能只靠连接断开）。"""
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._framer.reset()
        await self._run_cleanup()

    async def _pump(self, proc: asyncio.subprocess.Process) -> bool:
        """读流直到 EOF / 断流 / stop。返回是否收到过数据。"""
        got_data = False
        while self._running:
            chunk = await asyncio.wait_for(
                proc.stdout.read(READ_CHUNK), timeout=self.stall_timeout
            )
            if not chunk:
                break  # EOF：采集进程退出（下游统一走重连）
            got_data = True
            now = time.monotonic()
            self._last_data_at = now
            self._rate_window.append((now, len(chunk)))
            pcm = to_int16(chunk, self.bits)
            if not pcm:
                # 位深转换丢掉了整块（块长 < 4B 的尾部残片）：真实丢字节
                self.counters["align_errors"] += 1
                continue
            frames = self._framer.feed(pcm)
            if not frames:
                continue
            self.counters["frames"] += len(frames) // self.frame_bytes
            if self.on_frame is not None:
                try:
                    self.on_frame(frames)
                except Exception as exc:  # noqa: BLE001 - 消费方异常不拖垮采集
                    self.counters["callback_errors"] += 1
                    self.last_error = f"callback: {type(exc).__name__}: {exc}"
                    logger.warning(
                        f"[Sidecar:{self.name}] 帧回调异常: {exc}", module="XiaoAI"
                    )
        return got_data

    # ---- 健康面 ----

    def is_healthy(self) -> bool:
        """近端是否有新鲜字节（断流判定与消费侧回落共用同一判据）。"""
        return (
            self._running
            and self._last_data_at > 0
            and (time.monotonic() - self._last_data_at) <= self.stall_timeout
        )

    def stats(self) -> dict:
        now = time.monotonic()
        rate = 0
        cutoff = now - 5.0
        while self._rate_window and self._rate_window[0][0] < cutoff:
            self._rate_window.popleft()
        if self._rate_window:
            span = now - self._rate_window[0][0]
            total = sum(n for _, n in self._rate_window)
            if span > 0:
                rate = int(total / span)
        return {
            "enabled": True,
            "running": self._running,
            "healthy": self.is_healthy(),
            "frame_age": (
                round(now - self._last_data_at, 3) if self._last_data_at else None
            ),
            "bytes_per_sec": rate,
            "channels": self.channels,
            "reconnects": self.counters["reconnects"],
            "stalls": self.counters["stalls"],
            "spawn_failures": self.counters["spawn_failures"],
            "callback_errors": self.counters["callback_errors"],
            "cleanup_runs": self.counters["cleanup_runs"],
            "frames": self.counters["frames"],
            "align_errors": self.counters["align_errors"],
            "dropped_bytes": self._framer.counters["dropped_bytes"],
            "last_error": self.last_error,
        }
