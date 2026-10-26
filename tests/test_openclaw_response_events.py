import asyncio
import unittest
from unittest import mock

from core.openclaw import OpenClawManager


class OpenClawResponseEventsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved_texts = OpenClawManager._response_texts
        self.saved_events = OpenClawManager._response_events
        self.run_id = "response-events-test"
        self.waiter = asyncio.get_running_loop().create_future()
        OpenClawManager._response_texts = {}
        OpenClawManager._response_events = {self.run_id: self.waiter}

    async def asyncTearDown(self):
        if not self.waiter.done():
            self.waiter.cancel()
        OpenClawManager._response_texts = self.saved_texts
        OpenClawManager._response_events = self.saved_events

    async def emit(self, stream, data):
        await OpenClawManager._handle_agent_event({
            "event": "agent",
            "payload": {"runId": self.run_id, "stream": stream, "data": data},
        })
        await asyncio.sleep(0)

    async def test_initial_snapshot_followed_by_delta_only_events(self):
        await self.emit("assistant", {"text": "这", "delta": "这"})
        await self.emit("assistant", {"delta": "是一段完整的回复，"})
        await self.emit("assistant", {"delta": "不会只留下第一个字。"})
        self.assertEqual("这是一段完整的回复，不会只留下第一个字。",
                         OpenClawManager._response_texts[self.run_id])
        self.assertFalse(self.waiter.done())
        await self.emit("lifecycle", {"phase": "finishing"})
        self.assertFalse(self.waiter.done())
        await self.emit("lifecycle", {"phase": "end"})
        self.assertTrue(self.waiter.done())

    async def test_delta_only_from_first_event(self):
        await self.emit("assistant", {"delta": "完整"})
        await self.emit("assistant", {"delta": "回复"})
        self.assertEqual("完整回复", OpenClawManager._response_texts[self.run_id])
        self.assertFalse(self.waiter.done())

    async def test_snapshot_and_delta_do_not_duplicate_text(self):
        await self.emit("assistant", {"text": "Hello", "delta": "Hello"})
        await self.emit("assistant", {"delta": " world"})
        await self.emit("assistant", {"text": "Hello world!", "delta": "!"})
        self.assertEqual("Hello world!", OpenClawManager._response_texts[self.run_id])

    async def test_legacy_cumulative_snapshots_are_preserved(self):
        await self.emit("assistant", {"text": "Hello"})
        await self.emit("assistant", {"text": "Hello world"})
        await self.emit("assistant", {"delta": ""})
        self.assertEqual("Hello world", OpenClawManager._response_texts[self.run_id])

    async def test_orphan_run_output_is_rejected(self):
        """B-28 票 02/B1：runId 不在账（等待方已超时放弃）时事件不落账。"""
        orphan = "orphan-run"
        await OpenClawManager._handle_agent_event({
            "event": "run.output",
            "payload": {"runId": orphan, "output": {"text": "孤儿"}},
        })
        await OpenClawManager._handle_agent_event({
            "event": "run.completed",
            "payload": {"runId": orphan, "output": {"text": "迟到完整回复"}},
        })
        self.assertNotIn(orphan, OpenClawManager._response_texts)


class OpenClawEarlyEventTest(unittest.IsolatedAsyncioTestCase):
    """B-28 票 02/B5：ack 在途期早到的 run 事件挂起等定账，不静默丢弃。"""

    async def asyncSetUp(self):
        self.saved_texts = OpenClawManager._response_texts
        self.saved_events = OpenClawManager._response_events
        self.saved_idems = OpenClawManager._pending_idems
        self.saved_ack_timeout = OpenClawManager._ack_timeout
        OpenClawManager._response_texts = {}
        OpenClawManager._response_events = {}
        OpenClawManager._pending_idems = set()
        OpenClawManager._ack_timeout = 0.3

    async def asyncTearDown(self):
        OpenClawManager._response_texts = self.saved_texts
        OpenClawManager._response_events = self.saved_events
        OpenClawManager._pending_idems = self.saved_idems
        OpenClawManager._ack_timeout = self.saved_ack_timeout

    async def test_early_event_replayed_after_ack_books_runid(self):
        """ack 回来把 runId 入账后，早到事件按新键正常处理。"""
        OpenClawManager._pending_idems.add("idem-1")
        task = asyncio.create_task(OpenClawManager._handle_early_agent_event({
            "event": "run.completed",
            "payload": {"runId": "run-x", "output": {"text": "早到的完整回复"}},
        }))
        await asyncio.sleep(0.05)  # 早到事件进入等待循环
        # ack 定账：runId 入账、idem 退出在途集
        OpenClawManager._response_events["run-x"] = asyncio.get_running_loop().create_future()
        OpenClawManager._pending_idems.discard("idem-1")

        await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual("早到的完整回复", OpenClawManager._response_texts["run-x"])

    async def test_early_event_dropped_with_warning_when_ack_never_books(self):
        """ack 始终没来：超时后丢弃并留 WARNING，不悬挂。"""
        OpenClawManager._pending_idems.add("idem-1")
        task = asyncio.create_task(OpenClawManager._handle_early_agent_event({
            "event": "run.output",
            "payload": {"runId": "run-y", "output": {"text": "永远无人认领"}},
        }))
        await asyncio.wait_for(task, timeout=5.0)
        self.assertNotIn("run-y", OpenClawManager._response_texts)

    async def test_receiver_dispatches_early_event_only_when_idem_pending(self):
        """receiver 分派语义：runId 未入账 + idem 在途 → 走早到通道。"""
        dispatched: list[str] = []

        async def fake_handle(event_data):
            dispatched.append(event_data["payload"]["runId"])

        async def fake_early(event_data):
            dispatched.append("early:" + event_data["payload"]["runId"])

        with mock.patch.object(OpenClawManager, "_handle_agent_event", fake_handle), \
             mock.patch.object(OpenClawManager, "_handle_early_agent_event", fake_early):
            data = {"event": "run.output", "payload": {"runId": "run-z", "output": {"text": "x"}}}
            receiver = OpenClawManager._receiver_dispatch_agent_event

            # runId 已入账 → 直通
            OpenClawManager._response_events["run-z"] = asyncio.get_running_loop().create_future()
            OpenClawManager._pending_idems.clear()
            receiver(data)
            await asyncio.sleep(0)
            self.assertEqual(["run-z"], dispatched)

            # runId 未入账 + idem 在途 → 早到通道
            OpenClawManager._response_events.pop("run-z")
            OpenClawManager._pending_idems.add("idem-2")
            receiver(data)
            await asyncio.sleep(0)
            self.assertEqual(["run-z", "early:run-z"], dispatched)


if __name__ == "__main__":
    unittest.main()
