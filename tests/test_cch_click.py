"""CCH 历史点击 worker 契约测试（票 23，design §2.1/§2.6/§2.7，纯离线）。

覆盖：
- 归一化：括注后缀（「刘巧儿 (四段)」vs「刘巧儿」）、空白、大小写；
- 时间窗去重（design 主判据）：语音点歌后云端回声 ContentPlay → drop；
  窗外 / 异曲 → 放行；artist 单侧缺失按 name 判；
- 事件处理：drop 不触发播放；放行走 song_request.run(target=cch) 同一条链；
  worker 起播后自身占窗（spool 重复事件不二次起播）；
- worker 循环：clickpoll 拉取 → 逐事件处理；单事件异常不影响 worker；3 连败降频 5s；
- 云端 Pause 抢断（§2.71-3）：观察到 paused 才补一次 resume，非 paused 不动。

纯离线：cch_cast.musicctl / song_request.run / cch_np.snapshot 模块属性替换，
不联网、不连设备。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music import cch_cast  # noqa: E402
from core.music import cch_click  # noqa: E402
from core.music import cch_np  # noqa: E402


class CchClickTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        cch_click._recent.clear()
        cch_click._pending_ev = None
        if cch_click._pending_task is not None:
            cch_click._pending_task.cancel()
        cch_click._pending_task = None
        self._saved_musicctl = cch_cast.musicctl
        self._saved_host_cmd = cch_cast.host_cmd

    def tearDown(self):
        cch_click._recent.clear()
        cch_click._pending_ev = None
        if cch_click._pending_task is not None:
            cch_click._pending_task.cancel()
        cch_click._pending_task = None
        cch_cast.musicctl = self._saved_musicctl
        cch_cast.host_cmd = self._saved_host_cmd

    # ---- _norm ----

    def test_norm_strips_bracket_suffix_whitespace_case(self):
        self.assertEqual(cch_click._norm("刘巧儿 (四段)"), cch_click._norm("刘巧儿"))
        self.assertEqual(cch_click._norm(" Liu Er "), "liuer")
        self.assertEqual(cch_click._norm("（Live）"), "")

    # ---- dedupe（8s 时间窗，design 主判据）----

    def test_dedupe_drops_echo_within_window(self):
        fake_time = mock.Mock()
        fake_time.time.return_value = 1000.0
        with mock.patch.object(cch_click, "time", fake_time), \
             mock.patch.object(cch_click, "_cfg", lambda k, d=None: 8.0):
            cch_click.note_recent_play("刘巧儿 (四段)", "新凤霞")
            self.assertNotEqual(cch_click.dedupe("刘巧儿", "新凤霞", now=1005.0), "")
            # 窗外（>8s）→ 放行
            self.assertEqual(cch_click.dedupe("刘巧儿", "新凤霞", now=1008.5), "")

    def test_dedupe_allows_different_song(self):
        cch_click.note_recent_play("晴天", "周杰伦")
        self.assertEqual(cch_click.dedupe("七里香", "周杰伦", now=cch_click.time.time()), "")

    def test_dedupe_name_match_with_empty_artist(self):
        cch_click.note_recent_play("晴天", "")
        self.assertNotEqual(cch_click.dedupe("晴天", "周杰伦", now=cch_click.time.time()), "")

    # ---- _accept_event：去抖 last-tap-wins（票 23 实测双发形态）----
    # 时序确定性原则：真 sleep + resume noop 补丁（不 patch 全局 asyncio.sleep，
    # 那会把去抖计时与测试等待压成同一毫秒产生竞态）。

    async def test_accept_single_event_plays_after_debounce(self):
        seen = {}

        async def fake_run(args):
            seen.update(args)
            return "ok"

        async def noop_resume():
            return

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            cch_click._accept_event({"name": "晴天", "artist": "周杰伦", "source": "KWQQ"})
            t = cch_click._pending_task
            self.assertIsNotNone(t)
            await asyncio.wait_for(t, timeout=2)
        self.assertEqual(seen.get("query"), "晴天")
        self.assertEqual(seen.get("target"), "cch")
        self.assertIsNone(cch_click._pending_ev)

    async def test_accept_two_events_last_tap_wins(self):
        played = []

        async def fake_run(args):
            played.append(args.get("query"))
            return "ok"

        async def noop_resume():
            return

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.2), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            cch_click._accept_event({"name": "刘巧儿", "artist": "", "source": "KG"})
            t1 = cch_click._pending_task
            await asyncio.sleep(0.05)    # echo 先到；debounce=0.2s 未到期
            cch_click._accept_event({"name": "七里香", "artist": "", "source": "KG"})
            t2 = cch_click._pending_task
            self.assertIsNot(t1, t2, "新事件必须替换未决任务")
            await asyncio.wait_for(t2, timeout=2)   # 确定性等最后一条播完
        self.assertEqual(played, ["七里香"], "回声被替换，只播最后一条（刘巧儿未播）")

    async def test_play_event_clears_cch_state_single_shot(self):
        """点击播 = 单曲意图：play 后换代队列必须清空（防变体连播病灶）。"""
        sr = sys.modules.get("core.tools.song_request")
        if sr is None:
            from core.tools import song_request as sr
        sr._cch_state.update({"entry": 0, "items": [{"id": "x", "name": "刘巧儿"}]})

        async def fake_run(args):
            return "ok"

        async def noop_resume():
            return

        with mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            await cch_click._play_event(
                {"name": "晴天", "artist": "周杰伦", "source": "KWQQ"})
        self.assertEqual(sr._cch_state.get("items"), [])

    # ---- _play_event ----

    async def test_play_event_drops_on_recent_play(self):
        cch_click.note_recent_play("晴天", "周杰伦")
        called = []

        async def fake_run(args):
            called.append(args)
            return "ok"

        with mock.patch("core.tools.song_request.run", fake_run):
            await cch_click._play_event(
                {"name": "晴天", "artist": "周杰伦", "source": "KWQQ", "rid": ""})
        self.assertEqual(called, [], "echo 事件不得触发播放")

    async def test_play_event_plays_via_song_request_cch_chain(self):
        seen = {}

        async def fake_run(args):
            seen.update(args)
            return "已在 CCH 上为你播放《晴天》"

        real_sleep = asyncio.sleep

        async def quick_sleep(d, *a, **k):   # resume 补偿任务的 1.5s 等待即时完成
            await real_sleep(0.001)

        with mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(asyncio, "sleep", quick_sleep):
            await cch_click._play_event(
                {"name": "晴天", "artist": "周杰伦", "source": "KWQQ", "rid": "cloud-1"})
            await real_sleep(0.02)           # 让 fire-and-forget 的 resume 任务跑完
        self.assertEqual(seen.get("target"), "cch")
        self.assertEqual(seen.get("action"), "play")
        self.assertEqual(seen.get("query"), "晴天")
        self.assertEqual(seen.get("artist"), "周杰伦")
        # worker 自身起播也占窗：同一事件重复投递（CP+createlist 双发）被去重
        self.assertNotEqual(cch_click.dedupe("晴天", "周杰伦"), "")

    # ---- worker 循环 ----

    async def test_worker_polls_clickpoll_and_plays(self):
        calls = []

        async def fake_musicctl(op, body=None):
            calls.append((op, body))
            return True, {"events": [{"name": "歌一", "artist": "", "source": "KG"}],
                          "dropped": 0}

        played = []

        async def fake_run(args):
            played.append(args.get("query"))
            return "ok"

        with mock.patch.object(cch_cast, "musicctl", fake_musicctl), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05):
            task = asyncio.create_task(cch_click.run_worker())
            for _ in range(50):          # 确定性等 debounce+play 完成
                if played:
                    break
                await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(any(op == "clickpoll" for op, _ in calls))
        self.assertEqual(played, ["歌一"])

    async def test_worker_survives_event_and_poll_errors(self):
        state = {"n": 0}

        async def flaky_musicctl(op, body=None):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")
            return True, {"events": [], "dropped": 0}

        played = []

        async def fake_run2(args):
            played.append(args.get("query"))
            return "ok"

        real_sleep = asyncio.sleep

        async def quick_sleep(d, *a, **k):
            await real_sleep(0.001)

        with mock.patch.object(cch_cast, "musicctl", flaky_musicctl), \
             mock.patch("core.tools.song_request.run", fake_run2), \
             mock.patch.object(asyncio, "sleep", quick_sleep):
            task = asyncio.create_task(cch_click.run_worker())
            await real_sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(state["n"] >= 2, "poll 失败后继续轮询")

    async def test_worker_backs_off_after_3_failures(self):
        state = {"n": 0}

        async def flaky_musicctl(op, body=None):
            state["n"] += 1
            if state["n"] <= 3:
                raise RuntimeError("boom")
            return True, {"events": [], "dropped": 0}

        sleeps = []
        real_sleep = asyncio.sleep

        async def fake_sleep(d, *a, **k):
            sleeps.append(d)
            await real_sleep(0.001)

        with mock.patch.object(cch_cast, "musicctl", flaky_musicctl), \
             mock.patch.object(asyncio, "sleep", fake_sleep):
            task = asyncio.create_task(cch_click.run_worker())
            await real_sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(sleeps[:3], [cch_click._POLL_S, cch_click._POLL_S, cch_click._SLOW_POLL_S],
                         "3 连败后第 4 次等待降到 5s")

    # ---- 云端 Pause 抢断（§2.71-3）----

    async def test_resume_only_on_observed_pause(self):
        resumes = []

        async def fake_host_cmd(cmd, url=None, extra=None, timeout_s=5.0):
            resumes.append(cmd)
            return True, "OK"

        st_paused = {"state": {"extras": {"statusStr": "pause"}}}
        st_playing = {"state": {"extras": {"statusStr": "play"}}}
        real_sleep = asyncio.sleep

        async def quick_sleep(d, *a, **k):   # 跳过 1.5s 真等待
            await real_sleep(0.001)

        with mock.patch.object(cch_cast, "host_cmd", fake_host_cmd), \
             mock.patch.object(cch_np, "snapshot", return_value=st_paused), \
             mock.patch.object(asyncio, "sleep", quick_sleep):
            await cch_click._resume_if_paused()
        self.assertEqual(resumes, ["resume"], "观察到 paused 才补一次 resume")

        resumes.clear()
        with mock.patch.object(cch_cast, "host_cmd", fake_host_cmd), \
             mock.patch.object(cch_np, "snapshot", return_value=st_playing), \
             mock.patch.object(asyncio, "sleep", quick_sleep):
            await cch_click._resume_if_paused()
        self.assertEqual(resumes, [], "playing 不动")


if __name__ == "__main__":
    unittest.main()
