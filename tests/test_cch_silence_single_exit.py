"""方案 B 守门：CCH 静音物理出口唯一化。

三处调用者（preempt / focus_bridge / playback_session）必须都走
cch_cast.silence_for_new_intent()，不得各自直发 host_cmd("pause")。
"""
import unittest
from unittest import mock

from core.music import cch_cast, focus_bridge
from core.music.playback_session import PlaybackSession, PlaybackPhase


class CchSilenceSingleExitTest(unittest.IsolatedAsyncioTestCase):

    def tearDown(self):
        focus_bridge._reset_for_tests()

    async def test_unified_exit_pause_is_fire_and_forget(self):
        """统一出口的 pause 必须 wait_ack=False（11s 残留教训）。"""
        with mock.patch.object(cch_cast, "host_cmd",
                               new_callable=mock.AsyncMock,
                               return_value=(True, "SENT")) as hc:
            ok = await cch_cast.silence_for_new_intent()
        self.assertTrue(ok)
        hc.assert_awaited_once()
        self.assertEqual(hc.await_args.args[0], "pause")
        self.assertFalse(hc.await_args.kwargs.get("wait_ack", True))

    async def test_unified_exit_need_m5_adds_stop(self):
        """NEED_M5（实例已死）时补 stop 清场——沿用 playback_session 既有语义。"""
        with mock.patch.object(cch_cast, "host_cmd",
                               new_callable=mock.AsyncMock,
                               side_effect=[(False, "NEED_M5"), (True, "OK")]) as hc:
            await cch_cast.silence_for_new_intent()
        self.assertEqual([c.args[0] for c in hc.await_args_list], ["pause", "stop"])

    async def test_focus_cch_calls_unified_exit(self):
        """focus_bridge CCH 分支必须走统一出口，不再直发 host_cmd。"""
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
             mock.patch.object(cch_cast, "silence_for_new_intent",
                               new_callable=mock.AsyncMock,
                               return_value=True) as se:
            await focus_bridge.hold("conversation", renderer="cch")
        se.assert_awaited_once()


    async def test_playback_session_silence_calls_unified_exit(self):
        """playback_session.silence 走统一出口，CAS/phase 语义仍在。"""
        s = PlaybackSession(renderer_id="cch")
        s.mark_playing if False else None  # 无需 prepared，直接测 silence
        with mock.patch.object(cch_cast, "silence_for_new_intent",
                               new_callable=mock.AsyncMock,
                               return_value=True) as se:
            await s.silence()
        se.assert_awaited_once()
        self.assertEqual(s.phase, PlaybackPhase.SILENCED)
        self.assertEqual(s.intent_seq, 1)


if __name__ == "__main__":
    unittest.main()
