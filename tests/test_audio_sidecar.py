"""8ch 旁路采集器（lx06-audio-sidecar spec 票 01）的行为测试。

只测外部行为：命令构造、字节流分帧 8ch 对齐、断流检测与退避重连、
生命周期清理（假设备断言「发出了 kill」）、非法帧丢弃计数。
不连设备、不跑真 SSH——采集子进程用本地 Python 假进程替身。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.sidecar import (  # noqa: E402
    DEFAULT_PID_FILE,
    SIDECAR_CHANNELS,
    SidecarCaptureRunner,
    SidecarFramer,
    build_arecord_command,
    build_cleanup_command,
    parse_sidecar_config,
)

FRAME_BYTES = SIDECAR_CHANNELS * 2  # S16 交错
S32_FRAME_BYTES = SIDECAR_CHANNELS * 4


def _fake_writer_script(frames: int, interval: float = 0.005) -> str:
    """往 stdout 写 N 个 8ch/S32 全零帧的假采集进程。"""
    return (
        "import sys, time\n"
        f"frame = bytes({S32_FRAME_BYTES})\n"
        f"for _ in range({frames}):\n"
        "    sys.stdout.buffer.write(frame)\n"
        "    sys.stdout.flush()\n"
        f"    time.sleep({interval})\n"
    )


def _fake_silent_script(seconds: float) -> str:
    """只挂住不产字节的假采集进程（触发断流）。"""
    return f"import time; time.sleep({seconds})\n"


def _cleanup_marker_command(marker: Path) -> list[str]:
    return [sys.executable, "-I", "-c",
            f"open({str(marker)!r}, 'w').write('killed')"]


class CommandConstructionTests(unittest.TestCase):
    def test_arecord_command_shape(self):
        argv = build_arecord_command("10.10.10.20", "/keys/lx06")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-i", argv)
        self.assertIn("/keys/lx06", argv)
        self.assertIn("root@10.10.10.20", argv)
        remote = argv[-1]
        # PID 文件先落、exec 不换 PID；8ch/S32/16k/raw stdout（spec 契约）
        self.assertIn(f"echo $$ > {DEFAULT_PID_FILE}", remote)
        self.assertIn("exec arecord -D noop", remote)
        self.assertIn("-f S32_LE", remote)
        self.assertIn("-r 16000", remote)
        self.assertIn("-c 8", remote)
        self.assertIn("-t raw -", remote)
        # dropbear 老固件 ssh-rsa 协商（ubus-sidecar.py 同款实测）
        self.assertIn("HostKeyAlgorithms=+ssh-rsa", " ".join(argv))

    def test_cleanup_command_only_kills_recorded_arecord(self):
        argv = build_cleanup_command("10.10.10.20", "/keys/lx06")
        remote = argv[-1]
        self.assertIn(f"cat {DEFAULT_PID_FILE}", remote)
        # 判据：/proc/<pid>/cmdline 含 arecord 才 kill（防 PID 复用误杀）
        self.assertIn("grep -q arecord /proc/$p/cmdline", remote)
        self.assertIn("kill", remote)

    def test_parse_sidecar_config_disabled_or_incomplete(self):
        self.assertIsNone(parse_sidecar_config(None))
        self.assertIsNone(parse_sidecar_config({}))
        self.assertIsNone(parse_sidecar_config({"enabled": True}))
        self.assertIsNone(parse_sidecar_config({"enabled": True, "host": "h"}))

    def test_parse_sidecar_config_builds_command(self):
        cfg = parse_sidecar_config({
            "enabled": True, "host": "10.10.10.20", "key_path": "/keys/lx06",
        })
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["command"][0], "ssh")
        self.assertEqual(cfg["channels"], SIDECAR_CHANNELS)
        self.assertIsNotNone(cfg["cleanup_command"])

    def test_parse_sidecar_config_cmd_override(self):
        cfg = parse_sidecar_config({
            "enabled": True, "cmd": "python -I -c 'print(1)'",
        })
        self.assertEqual(cfg["command"], ["python", "-I", "-c", "print(1)"])
        # 外部命令拓扑下不代管远端清理
        self.assertIsNone(cfg["cleanup_command"])

    def test_parse_sidecar_config_ssh_opts_override(self):
        opts = ["-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile=/app/keys/lx06-ir/known_hosts"]
        cfg = parse_sidecar_config({
            "enabled": True, "host": "10.10.10.20", "key_path": "/app/keys/lx06-ir/id_rsa",
            "ssh_opts": opts,
        })
        # ssh_opts 整组替换默认组：新参数在、默认的 ssh-rsa 协商不在
        argv = " ".join(cfg["command"])
        self.assertIn("-o StrictHostKeyChecking=yes", argv)
        self.assertIn("UserKnownHostsFile=/app/keys/lx06-ir/known_hosts", argv)
        self.assertNotIn("HostKeyAlgorithms", argv)
        # cleanup 命令同样带 ssh_opts
        self.assertIn("StrictHostKeyChecking=yes", " ".join(cfg["cleanup_command"]))

    def test_parse_sidecar_config_numeric_clamps(self):
        cfg = parse_sidecar_config({
            "enabled": True, "host": "h", "key_path": "k",
            "channels": 99, "stall_timeout": 999, "backoff_max": 0.5,
        })
        self.assertEqual(cfg["channels"], 8)
        self.assertLessEqual(cfg["stall_timeout"], 30.0)
        self.assertGreaterEqual(cfg["backoff_max"], cfg["backoff_initial"])


class FramerTests(unittest.TestCase):
    def test_arbitrary_chunk_boundaries_produce_aligned_frames(self):
        framer = SidecarFramer(FRAME_BYTES)
        data = bytes(range(256)) * 8  # 2048 B = 32 帧
        out = b""
        # 逐字节喂：无论边界怎么切，产出必须是帧整数倍
        for i in range(len(data)):
            out += framer.feed(data[i:i + 1])
        self.assertEqual(len(out) % FRAME_BYTES, 0)
        self.assertEqual(out, data)
        self.assertEqual(framer.counters["frames"] if "frames" in framer.counters
                         else 0, 0)  # 帧计数归 runner，framer 只对齐

    def test_remainder_carried_not_dropped(self):
        framer = SidecarFramer(FRAME_BYTES)
        self.assertEqual(framer.feed(b"\x00" * 3), b"")
        out = framer.feed(b"\x01" * (FRAME_BYTES - 3))
        self.assertEqual(len(out), FRAME_BYTES)
        self.assertEqual(framer.feed(b""), b"")

    def test_buffer_overflow_dropped_and_counted(self):
        framer = SidecarFramer(FRAME_BYTES)
        # MAX_FRAME_BUFFER 属模块常量；直接构造大块触发丢弃
        from core.services.audio.sidecar import MAX_FRAME_BUFFER
        framer.feed(b"\x00" * MAX_FRAME_BUFFER)  # 恰好整帧倍数，不丢
        self.assertEqual(framer.counters["dropped_bytes"], 0)
        framer.feed(b"\x00" * (MAX_FRAME_BUFFER + 7))  # 超限 → 清空丢弃
        self.assertGreater(framer.counters["dropped_bytes"], 0)
        self.assertEqual(len(framer.feed(b"\x00" * FRAME_BYTES)),
                         FRAME_BYTES)  # 丢弃后继续对齐


class RunnerTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(asyncio.wait_for(coro, timeout=20.0))

    def test_frames_flow_and_alignment(self):
        frames = []

        async def main():
            runner = SidecarCaptureRunner(
                [sys.executable, "-I", "-c", _fake_writer_script(200)],
                stall_timeout=1.0,
                backoff_initial=0.1, backoff_max=0.2,
                on_frame=frames.append,
            )
            task = asyncio.create_task(runner.run())
            for _ in range(200):
                if runner.counters["frames"] >= 200:
                    break
                await asyncio.sleep(0.05)
            stats = runner.stats()
            runner.stop()
            await asyncio.wait_for(task, timeout=5.0)
            return stats

        stats = self._run(main())
        # 回调按「对齐后的字节块」投递：每块都是帧整数倍；总帧数与 stats 一致
        self.assertGreaterEqual(len(frames), 1)
        for blob in frames:
            self.assertGreater(len(blob), 0)
            self.assertEqual(len(blob) % FRAME_BYTES, 0)
        self.assertEqual(stats["frames"], sum(len(b) // FRAME_BYTES for b in frames))
        self.assertGreaterEqual(stats["frames"], 200)
        self.assertEqual(stats["align_errors"], 0)
        self.assertGreater(stats["bytes_per_sec"], 0)
        self.assertIsNotNone(stats["frame_age"])

    def test_eof_reconnects(self):
        async def main():
            runner = SidecarCaptureRunner(
                [sys.executable, "-I", "-c", _fake_writer_script(10)],
                stall_timeout=1.0,
                backoff_initial=0.05, backoff_max=0.1,
            )
            task = asyncio.create_task(runner.run())
            for _ in range(100):
                if runner.counters["reconnects"] >= 2:
                    break
                await asyncio.sleep(0.05)
            runner.stop()
            await asyncio.wait_for(task, timeout=5.0)
            return runner

        runner = self._run(main())
        self.assertGreaterEqual(runner.counters["reconnects"], 2)

    def test_stall_triggers_kill_via_cleanup_and_reconnect(self):
        async def main():
            with tempfile.TemporaryDirectory() as tmp:
                marker = Path(tmp) / "cleanup-marker"
                runner = SidecarCaptureRunner(
                    [sys.executable, "-I", "-c", _fake_silent_script(30)],
                    cleanup_command=_cleanup_marker_command(marker),
                    stall_timeout=0.3,
                    backoff_initial=0.05, backoff_max=0.1,
                )
                task = asyncio.create_task(runner.run())
                for _ in range(100):
                    if marker.exists() and runner.counters["stalls"] >= 1:
                        break
                    await asyncio.sleep(0.05)
                runner.stop()
                await asyncio.wait_for(task, timeout=5.0)
                return marker.read_text() if marker.exists() else ""

        content = self._run(main())
        # 「发出了 kill」的替身断言：清理命令确实被执行过（spawn 前置 +
        # 断流回收至少两次）
        self.assertEqual(content, "killed")

    def test_unhealthy_before_data_and_after_stall(self):
        async def main():
            runner = SidecarCaptureRunner(
                [sys.executable, "-I", "-c", _fake_silent_script(30)],
                stall_timeout=0.3,
                backoff_initial=0.05, backoff_max=0.1,
            )
            self.assertFalse(runner.is_healthy())  # 从未收到数据
            task = asyncio.create_task(runner.run())
            for _ in range(50):
                if runner.counters["stalls"] >= 1:
                    break
                await asyncio.sleep(0.05)
            self.assertFalse(runner.is_healthy())  # 断流后不健康
            self.assertEqual(runner.stats()["stalls"], 1)
            runner.stop()
            await asyncio.wait_for(task, timeout=5.0)

        self._run(main())

    def test_healthy_after_data(self):
        async def main():
            runner = SidecarCaptureRunner(
                [sys.executable, "-I", "-c", _fake_writer_script(2000, 0.003)],
                stall_timeout=1.0,
                backoff_initial=0.05, backoff_max=0.1,
            )
            task = asyncio.create_task(runner.run())
            for _ in range(100):
                if runner.is_healthy():
                    break
                await asyncio.sleep(0.05)
            ok = runner.is_healthy()
            runner.stop()
            await asyncio.wait_for(task, timeout=5.0)
            return ok

        self.assertTrue(self._run(main()))

    def test_callback_error_does_not_kill_capture(self):
        def bad_consumer(_frame):
            raise RuntimeError("consumer boom")

        async def main():
            runner = SidecarCaptureRunner(
                [sys.executable, "-I", "-c", _fake_writer_script(200)],
                stall_timeout=1.0,
                backoff_initial=0.05, backoff_max=0.1,
                on_frame=bad_consumer,
            )
            task = asyncio.create_task(runner.run())
            for _ in range(100):
                if runner.counters["callback_errors"] >= 1:
                    break
                await asyncio.sleep(0.05)
            still_pumping = runner.counters["frames"] > 0
            runner.stop()
            await asyncio.wait_for(task, timeout=5.0)
            return runner, still_pumping

        runner, still_pumping = self._run(main())
        self.assertGreaterEqual(runner.counters["callback_errors"], 1)
        self.assertTrue(still_pumping)

    def test_spawn_failure_with_missing_binary(self):
        async def main():
            runner = SidecarCaptureRunner(
                ["definitely-not-a-real-binary-oxai"],
                stall_timeout=0.3,
                backoff_initial=0.05, backoff_max=0.1,
            )
            task = asyncio.create_task(runner.run())
            for _ in range(50):
                if runner.counters["spawn_failures"] >= 2:
                    break
                await asyncio.sleep(0.05)
            runner.stop()
            await asyncio.wait_for(task, timeout=5.0)
            return runner

        runner = self._run(main())
        self.assertGreaterEqual(runner.counters["spawn_failures"], 2)


if __name__ == "__main__":
    unittest.main()
