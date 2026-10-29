"""song_request Agent 工具（票 01，kw-qq-music-api 点歌 spike）契约测试。

覆盖（票面施工要点 9）：

  1. 注册表契约：NAME/TOOL_SPEC/RISK=a/validate，注册 + 外网工具记账口径；
  2. enabled=false 时 run 返回未启用说明（配置闸门）；
  3. play 全路径：envelope 业务失败 / 搜索无结果 / 首源失败回落下一源 /
     ekey 加密件忽略（headers 不进播放层）；
  4. 队列 next/prev/stop 就地切播与状态迁移（技能内内存队列）；
  5. 播报文案不含 KWS 词表词（deploy 真实数据面交叉检查）；
  6. 播放发起后完成回读（incident-music-next-silent-drop ③：发起成功 ≠ 出声）。

HTTP 层一律打桩（_http_get_json），设备层用内存假 speaker，不碰真网。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for _path in (ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# core.services.speaker（经 registry 间接 import）import 期需要原生扩展占位
sys.modules.setdefault("open_xiaoai_server", __import__("types").SimpleNamespace())

from core.tools import registry as tools_registry  # noqa: E402
from core.tools import song_request  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _NoopAnnounce:
    """假 announce 上下文：记录文案、立即退出（不模拟等待）。"""

    def __init__(self, speaker, text):
        self.speaker = speaker
        self.text = text

    async def __aenter__(self):
        self.speaker.announce_texts.append(self.text)
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSpeaker:
    """内存假 speaker：只实现 song_request 用到的面。"""

    def __init__(self, playing: str = "paused", start_playing: bool = True, device_queue: bool = True):
        self.play_calls: list[dict] = []
        self.announce_texts: list[str | None] = []
        self.stop_calls = 0
        self.playing_states: list[str] = [playing]  # get_playing 依次返回
        self.announce_error: Exception | None = None  # 只拒一次（模拟词表拒绝）
        self.start_playing = start_playing  # False 模拟 URL 源不可达（状态不翻转）
        self.device_queue = device_queue  # 设备自身队列是否可切（next/prev 回落用）
        self.next_calls = 0
        self.prev_calls = 0

    def announce(self, text=None, *, reason="announce", **kw):
        if self.announce_error is not None:
            err, self.announce_error = self.announce_error, None
            raise err
        return _NoopAnnounce(self, text)

    async def play(self, text=None, url=None, blocking=True, **kw):
        self.play_calls.append({"text": text, "url": url, "blocking": blocking})
        if url is not None and not blocking and self.start_playing:
            # URL 起播即视为设备进入 playing（回读可见）
            self.playing_states.append("playing")
        return True

    async def stop_device_audio(self) -> None:
        self.stop_calls += 1
        self.playing_states.append("paused")

    async def get_playing(self, sync=False):
        return self.playing_states[-1]

    async def set_playing(self, playing=True):
        self.playing_states.append("playing" if playing else "paused")
        return True

    async def next_track(self):
        self.next_calls += 1
        return self.device_queue

    async def prev_track(self):
        self.prev_calls += 1
        return self.device_queue


def _envelope(code: int, data, msg: str = "ok") -> dict:
    return {"code": code, "msg": msg, "data": data, "timestamp": 0}


def _search_item(name="晴天", singer="周杰伦", source="kuwo", sid="228908") -> dict:
    return {
        "id": sid,
        "name": name,
        "singer": singer,
        "album": "叶惠美",
        "ext": "mp3",
        "size_bytes": 9_000_000,
        "duration_s": 269,
        "cover": "http://cover/1.jpg",
        "source": source,
        "extra": {},
    }


class RegistryContractTest(unittest.TestCase):
    """要点 1/2：模块契约与注册表接线。"""

    def test_module_contract(self):
        self.assertEqual("song_request", song_request.NAME)
        self.assertEqual("song_request", song_request.TOOL_SPEC["function"]["name"])
        self.assertEqual("a", song_request.RISK)  # 复述即执行（spec 裁决 1）
        self.assertTrue(callable(song_request.run))
        self.assertTrue(callable(getattr(song_request, "validate", None)))

    def test_registered_and_visible(self):
        self.assertIs(song_request, tools_registry._REGISTRY.get("song_request"))
        names = [
            spec["function"]["name"] for spec in tools_registry.get_all_tools()
        ]
        self.assertIn("song_request", names)

    def test_external_network_accounting(self):
        # 点歌要出公网搜歌（kw-qq-api 在 NAS，但搜索属联网取数），计入联网口径
        self.assertIn("song_request", tools_registry.EXTERNAL_NETWORK_TOOLS)
        self.assertTrue(tools_registry.is_external_network_tool("song_request"))

    def test_tool_spec_parameters(self):
        props = song_request.TOOL_SPEC["function"]["parameters"]["properties"]
        self.assertIn("action", props)
        self.assertIn("query", props)
        self.assertIn("artist", props)
        self.assertIn("source", props)
        self.assertIn(
            "album", props["action"]["enum"], "专辑连播（P6 /album）须在工具枚举内"
        )
        self.assertEqual(["action"], song_request.TOOL_SPEC["function"]["parameters"]["required"])


def _patch_cfg(**overrides):
    """把 song_request._cfg 换成内存表（测试不落真配置）。"""

    def _fake(key, default=None):
        return overrides.get(key, default)

    return mock.patch.object(song_request, "_cfg", side_effect=_fake)


def _reset_state():
    song_request._cancel_advance()
    song_request._state.update(
        {
            "current": None,
            "history": [],
            "candidates": [],
            "index": -1,
            "album": False,
            "started_at": 0.0,
        }
    )


class _ApiStub:
    """按 (path 关键字, source) 出 envelope body 的传输层桩（冒充 _fetch）。

    responses: list[ (path_contains, source, envelope_or_None) ] 顺序匹配；
    data 为 Exception 实例时抛出（模拟传输异常）；data 为 callable 时按
    (url, params) 现算（支持按请求参数如 id 出值）。调用留痕供断言。
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, session, url: str, params: dict):
        self.calls.append((url, dict(params)))
        # url 形如 http://api.test:3003/{source}/search —— source 取第 4 段
        parts = url.split("/")
        source = parts[3] if len(parts) > 4 else ""
        for pat, src, data in self.responses:
            if pat in url and (src is None or src == source):
                if isinstance(data, Exception):
                    raise data
                if callable(data):
                    data = data(url, params)
                return data
        return None

    def calls_for(self, keyword: str) -> list[tuple[str, dict]]:
        return [c for c in self.calls if keyword in c[0]]


def _patch_speaker(speaker):
    return mock.patch.object(song_request, "get_speaker", return_value=speaker)


def _play_env(api, speaker, **cfg):
    merged = {
        "enabled": True,
        "api_base": "http://10.10.10.2:3003",
        "sources": ["kuwo", "netease", "qq"],
    }
    merged.update(cfg)
    return (
        mock.patch.object(song_request, "_fetch", api),
        _patch_cfg(**merged),
        _patch_speaker(speaker),
        mock.patch.object(song_request, "_CONFIRM_INTERVAL_S", 0.0),
    )


class ApiContractTest(unittest.TestCase):
    """envelope 契约（API-REFERENCE §3）：code != 200 即业务失败。"""

    def _with_fetch(self, fetch_body):
        async def _fake_fetch(session, url, params):
            return fetch_body

        async def _main():
            async with song_request.aiohttp.ClientSession() as session:
                with mock.patch.object(song_request, "_fetch", _fake_fetch), \
                        _patch_cfg(api_base="http://api.test:3003"):
                    return await song_request._api(session, "/kuwo/search", {})

        return _run(_main())

    def test_code_200_returns_data(self):
        data = self._with_fetch(_envelope(200, [{"id": "1"}]))
        self.assertEqual([{"id": "1"}], data)

    def test_code_404_is_business_failure(self):
        # HTTP 200 但 code=404：业务失败，不得当成功
        self.assertIsNone(self._with_fetch(_envelope(404, None, msg="歌曲无资源")))

    def test_non_dict_body_is_failure(self):
        self.assertIsNone(self._with_fetch(["not", "envelope"]))
        self.assertIsNone(self._with_fetch(None))

    def test_transport_none_is_failure(self):
        self.assertIsNone(self._with_fetch(None))


class _FakeResp:
    def __init__(self, status=200, body=None, json_error=None):
        self.status = status
        self._body = body
        self._json_error = json_error

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._body


class _FakeSession:
    """假 aiohttp session：get 返回预设响应或抛异常。"""

    def __init__(self, resp=None, get_error=None):
        self._resp = resp
        self._get_error = get_error

    def get(self, url, params=None, timeout=None):
        if self._get_error is not None:
            raise self._get_error

        class _Ctx:
            async def __aenter__(self_inner):
                return self._resp

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class FetchLayerTest(unittest.TestCase):
    """传输层收敛（weather 模式）：任何异常/非 200 都收敛为 None，不击穿。"""

    def _fetch(self, session):
        return _run(song_request._fetch(
            session, "http://api.test:3003/kuwo/search", {"keywords": "x"}
        ))

    def test_http_500_is_none(self):
        self.assertIsNone(self._fetch(_FakeSession(_FakeResp(status=500))))

    def test_network_error_is_none(self):
        self.assertIsNone(self._fetch(_FakeSession(get_error=OSError("boom"))))

    def test_timeout_is_none(self):
        self.assertIsNone(self._fetch(_FakeSession(get_error=asyncio.TimeoutError())))

    def test_json_decode_error_is_none(self):
        self.assertIsNone(
            self._fetch(_FakeSession(_FakeResp(json_error=ValueError("bad json"))))
        )

    def test_ok_body_passthrough(self):
        body = _envelope(200, [])
        self.assertEqual(body, self._fetch(_FakeSession(_FakeResp(body=body))))


class PlayPathTest(unittest.TestCase):
    """要点 3/4/6：两跳搜歌、announce 起播、完成回读、源策略与 ekey 忽略。"""

    def setUp(self):
        _reset_state()
        from core.tools.registry import reset_reply_suppression

        reset_reply_suppression()

    def test_play_suppresses_turn_reply(self):
        # 工具已播报并起播：置「本轮免播」，避免收尾 TTS 顶掉音乐
        from core.tools.registry import reply_suppressed

        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [_search_item()])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/1.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertTrue(reply_suppressed())

    def test_failed_play_keeps_turn_reply(self):
        # 未起播就不许吞掉本轮播报（失败原因要让用户听到）
        from core.tools.registry import reply_suppressed

        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [])),
            ("search", "netease", _envelope(200, [])),
            ("search", "qq", _envelope(200, [])),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            _run(song_request.run({"action": "play", "query": "乱写的词"}))
        self.assertFalse(reply_suppressed())

    def test_play_happy_path(self):
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [_search_item()])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/1.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))

        self.assertIn("晴天", out)
        self.assertIn("周杰伦", out)
        self.assertIn("kuwo", out)
        self.assertIn("已确认出声", out)
        plays = [p for p in speaker.play_calls if p["url"]]
        self.assertEqual(1, len(plays))
        self.assertFalse(plays[0]["blocking"])  # 非阻塞起播，回读另行确认
        self.assertIn("晴天", speaker.announce_texts[0] or "")
        # 队列状态落位
        self.assertEqual("晴天", song_request._state["current"]["name"])
        # 契约：quality 固定 320k
        url_calls = api.calls_for("song/url")
        self.assertEqual("320k", url_calls[0][1].get("quality"))
        # 契约：search 带 keywords
        search_calls = api.calls_for("search")
        self.assertEqual("晴天", search_calls[0][1].get("keywords"))

    def test_envelope_business_failure_no_play(self):
        # code != 200 = 业务失败（HTTP 200 不代表成功），不得起播
        api = _ApiStub([
            ("search", "kuwo", _envelope(404, None, msg="歌曲无资源")),
            ("search", "netease", _envelope(404, None, msg="歌曲无资源")),
            ("search", "qq", _envelope(404, None, msg="歌曲无资源")),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "冷门曲"}))
        self.assertIn("没有找到", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])

    def test_search_no_results(self):
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [])),
            ("search", "netease", _envelope(200, [])),
            ("search", "qq", _envelope(200, [])),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "乱写的词"}))
        self.assertIn("没有找到", out)

    def test_search_items_envelope(self):
        # 真机形状（2026-10-27）：data = {keywords,page,total,items:[...]}，
        # 不是裸列表——曾因只认 list 导致三源全部「搜不到」
        page = {"keywords": "晴天", "page": 1, "total": 1, "items": [_search_item()]}
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, page)),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/1.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))

        self.assertIn("kuwo", out)
        self.assertIn("已确认出声", out)
        self.assertEqual(["http://cdn/1.mp3"], [p["url"] for p in speaker.play_calls if p["url"]])

    def test_search_dict_without_items_is_empty(self):
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, {"keywords": "x", "total": 0})),
            ("search", "netease", _envelope(200, {"keywords": "x", "total": 0})),
            ("search", "qq", _envelope(200, {"keywords": "x", "total": 0})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("没有找到", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])

    def test_versions_collapse_in_queue(self):
        # 同一首歌的多个版本不得占多个队列位（真机 2026-10-27：下一首播同一首）
        items = [
            _search_item(),
            _search_item(name="晴天 (Live)", sid="2"),
            _search_item(name="晴天(深情版)", singer="Lucky小爱", sid="3"),
            _search_item(name="晴天+告白气球 (Live)", sid="4"),
        ]

        def _by_query(url, params):
            if params.get("keywords") == "晴天":
                return _envelope(200, {"keywords": "晴天", "total": 3, "items": items})
            return _envelope(200, {"keywords": "周杰伦", "total": 1,
                                   "items": [_search_item(name="七里香", sid="7")]})

        api = _ApiStub([
            ("search", "kuwo", _by_query),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/1.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            _run(song_request.run({"action": "play", "query": "晴天"}))

        # 三个版本合并为一首，再按歌手补出别的歌
        self.assertEqual(
            ["晴天", "七里香"],
            [c["name"] for c in song_request._state["candidates"]],
        )

    def test_queue_excludes_variants_under_multiword_query(self):
        # LLM 常传「周杰伦 晴天」：拼接曲不以该检索词开头，仍须按当前曲名剔除
        items = [_search_item(), _search_item(name="晴天+告白气球 (Live)", sid="2")]
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, {"keywords": "x", "items": items})),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/1.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            _run(song_request.run({"action": "play", "query": "周杰伦 晴天"}))

        names = [c["name"] for c in song_request._state["candidates"]]
        self.assertEqual(["晴天"], names)

    def test_single_track_extends_by_singer(self):
        # 候选只剩同一首时，用歌手再搜一轮补别的歌，队列才有意义
        def _by_query(url, params):
            if params.get("keywords") == "晴天":
                return _envelope(200, {"keywords": "晴天", "total": 1,
                                       "items": [_search_item()]})
            return _envelope(200, {"keywords": "周杰伦", "total": 2,
                                   "items": [
                                       _search_item(name="七里香", sid="7"),
                                       # 歌手检索里混进来的拼接版必须再被过滤
                                       _search_item(name="晴天+告白气球 (Live)", sid="8"),
                                   ]})

        api = _ApiStub([
            ("search", "kuwo", _by_query),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/1.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            _run(song_request.run({"action": "play", "query": "晴天"}))

        names = [c["name"] for c in song_request._state["candidates"]]
        self.assertEqual(["晴天", "七里香"], names)

    def test_first_source_failure_falls_back(self):
        # kuwo 传输层失败（_api 返回 None）→ netease 接管
        api = _ApiStub([
            ("search", "kuwo", None),
            ("search", "netease", _envelope(200, [_search_item(source="netease", sid="108914")])),
            ("song/url", "netease", _envelope(200, {"url": "http://cdn/2.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("netease", out)
        self.assertIn("已确认出声", out)
        # 回落即停：kuwo 失败一跳 + netease 成功（另有一次同源按歌手补歌，见
        # _extend_with_singer），qq 不得再空转
        search_urls = [u for u, _ in api.calls_for("search")]
        self.assertFalse([u for u in search_urls if "qq" in u])
        self.assertEqual("kuwo", search_urls[0].split("/")[3])
        self.assertTrue(all("netease" in u for u in search_urls[1:]))

    def test_ekey_item_ignored(self):
        # ADR-0002：ekey 加密件不进播放层——视为该源无可用直链
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [_search_item()])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/x.mflac", "ekey": "AAAA"})),
            ("search", "netease", _envelope(200, [_search_item(source="netease", sid="108914")])),
            ("song/url", "netease", _envelope(200, {"url": "http://cdn/2.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("netease", out)
        # mflac 加密件从未被送去播放
        played = [p["url"] for p in speaker.play_calls if p["url"]]
        self.assertEqual(["http://cdn/2.mp3"], played)

    def test_ekey_only_means_no_usable_url(self):
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [_search_item()])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/x.mflac", "ekey": "AAAA"})),
            ("search", "netease", _envelope(200, [])),
            ("search", "qq", _envelope(200, [])),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("没有找到", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])

    def test_migu_copyright_passthrough(self):
        # 契约：migu 的 extra.copyrightId 须以 ?copyright= 回传 song/url
        item = _search_item(source="migu", sid="6005")
        item["extra"] = {"copyrightId": "699071"}
        api = _ApiStub([
            ("search", "migu", _envelope(200, [item])),
            ("song/url", "migu", _envelope(200, {"url": "http://cdn/3.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker, sources=["migu"])
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天", "source": "migu"}))
        self.assertIn("已确认出声", out)
        url_params = api.calls_for("song/url")[0][1]
        self.assertEqual("699071", url_params.get("copyright"))

    def test_bilibili_source_rejected(self):
        # 票面裁决：排除 bilibili（headers 无通道 ⇒ 403），任何配置下不可达
        api = _ApiStub([])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker, sources=["bilibili"])
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天", "source": "bilibili"}))
        self.assertIn("bilibili", out)
        self.assertEqual([], api.calls)

    def test_source_outside_config_rejected(self):
        api = _ApiStub([])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)  # 默认 kuwo/netease/qq
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "x", "source": "kugou"}))
        self.assertIn("未启用", out)
        self.assertEqual([], api.calls)

    def test_default_sources_exclude_bilibili_and_migu(self):
        # 要点 6：默认段只有 kuwo/netease/qq；migu 留配置可加
        self.assertEqual(("kuwo", "netease", "qq"), song_request._DEFAULT_SOURCES)

    def test_missing_speaker(self):
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [_search_item()])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/1.mp3"})),
        ])
        patches = _play_env(api, None)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("音箱", out)
        self.assertFalse([p for p in FakeSpeaker().play_calls])

    def test_confirm_playing_tolerates_transient_readback_error(self):
        # 单次瞬时回读异常只消耗一轮计数、下一轮重试；恢复后仍 confirmed
        speaker = FakeSpeaker()
        calls = {"n": 0}

        async def flaky_get(sync=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient readback failure")
            return "playing"

        with mock.patch.object(speaker, "get_playing", flaky_get), \
                mock.patch.object(song_request, "_CONFIRM_INTERVAL_S", 0.0):
            ok = _run(song_request._confirm_playing(speaker))
        self.assertTrue(ok)
        self.assertEqual(2, calls["n"])

    def test_readback_not_playing_caveat(self):
        # incident-music-next-silent-drop ③：发起成功 ≠ 出声，超时兜底要留痕
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [_search_item()])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/1.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker(playing="paused", start_playing=False)
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("未确认出声", out)
        self.assertIn("晴天", out)

    def test_announce_rejected_falls_back_to_generic(self):
        # 要点 8：歌名/歌手撞 KWS 词表时降级为通用文案，起播不中断
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [_search_item()])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/1.mp3", "ext": "mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        speaker_module = __import__("tests.fake_device", fromlist=["load_speaker_module"]).load_speaker_module()
        speaker.announce_error = speaker_module.AnnounceRejected("停止音乐")
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("已确认出声", out)
        played = [p["url"] for p in speaker.play_calls if p["url"]]
        self.assertEqual(["http://cdn/1.mp3"], played)
        # 第二次尝试用了不含曲名的通用文案
        self.assertIn(song_request._FALLBACK_ANNOUNCE, speaker.announce_texts)

    def test_url_hop_failure_falls_back_to_next_source(self):
        # kuwo 搜到但 song/url 传输失败 → 候选弃用 → netease 接管
        api = _ApiStub([
            ("search", "kuwo", _envelope(200, [_search_item()])),
            ("song/url", "kuwo", None),
            ("search", "netease", _envelope(200, [_search_item(source="netease", sid="108914")])),
            ("song/url", "netease", _envelope(200, {"url": "http://cdn/2.mp3"})),
        ])
        speaker = FakeSpeaker()
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("netease", out)
        self.assertIn("已确认出声", out)


class GateAndValidateTest(unittest.TestCase):
    """要点 7：enabled 配置闸门；validate：action 白名单 + play 必带 query。"""

    def test_disabled_by_default(self):
        # 无任何配置（default None）时必须按未启用处理——保守缺省
        with _patch_cfg():
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("未启用", out)
        self.assertIn("song_request", out)

    def test_enabled_false_rejects(self):
        with _patch_cfg(enabled=False):
            out = _run(song_request.run({"action": "play", "query": "晴天"}))
        self.assertIn("未启用", out)

    def test_invalid_action_rejected(self):
        with _patch_cfg(enabled=True):
            out = _run(song_request.run({"action": "seek", "query": "x"}))
        self.assertIn("play/album/next/prev/pause/resume/stop", out)

    def test_play_without_query_rejected(self):
        with _patch_cfg(enabled=True):
            out = _run(song_request.run({"action": "play"}))
        self.assertIn("query", out)

    def test_album_without_query_rejected(self):
        with _patch_cfg(enabled=True):
            out = _run(song_request.run({"action": "album"}))
        self.assertIn("query", out)

    def test_validate_direct(self):
        self.assertIsNone(song_request.validate({"action": "stop"}))
        self.assertIsNone(song_request.validate({"action": "play", "query": "晴天"}))
        self.assertIsNone(song_request.validate({"action": "album", "query": "叶惠美"}))
        self.assertIsNone(song_request.validate({"action": "pause"}))
        self.assertIsNone(song_request.validate({"action": "resume"}))
        self.assertIsNotNone(song_request.validate({"action": "play"}))
        self.assertIsNotNone(song_request.validate({"action": "album"}))
        self.assertIsNotNone(song_request.validate({"action": "seek"}))
        self.assertIsNotNone(song_request.validate(None))


def _seed_state(current=None, candidates=None, index=-1, history=None, album=False, started_at=0.0):
    song_request._state.update(
        {
            "current": current,
            "candidates": candidates or [],
            "index": index,
            "history": list(history or []),
            "album": album,
            "started_at": started_at,
        }
    )


class QueueTest(unittest.TestCase):
    """要点 5：模块级内存队列——next/prev 就地切 URL，stop 走设备停播。"""

    def setUp(self):
        _reset_state()

    def _act(self, api, speaker, args):
        patches = _play_env(api, speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            return _run(song_request.run(args))

    def test_has_queue_reflects_state(self):
        # 原生唤醒的队列控制词路由依赖该判定（无队列必须放行原生）
        self.assertFalse(song_request.has_queue())
        _seed_state(current=_search_item(), candidates=[_search_item()], index=0)
        self.assertTrue(song_request.has_queue())

    def test_stop_calls_device_stop_and_clears_state(self):
        from core.tools.registry import reply_suppressed

        _seed_state(current=_search_item(), index=0, history=[_search_item(sid="9")])
        speaker = FakeSpeaker()
        api = _ApiStub([])
        out = self._act(api, speaker, {"action": "stop"})
        self.assertEqual(1, speaker.stop_calls)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])
        self.assertIsNone(song_request._state["current"])
        self.assertEqual([], song_request._state["candidates"])
        # 文案称「队列已清空」——历史栈也要清，否则 stop 后 prev 仍能恢复播放
        self.assertEqual([], song_request._state["history"])
        self.assertIn("停止", out)
        # 停止即免播：不再插一句确认，本轮直接退出会话（否则会话吊在聆听）
        self.assertTrue(reply_suppressed())

    def test_stop_with_empty_state_still_stops_device(self):
        # 队列空也要能停——设备上可能还在放别的来源（截胡/URL 直播）
        speaker = FakeSpeaker()
        api = _ApiStub([])
        out = self._act(api, speaker, {"action": "stop"})
        self.assertEqual(1, speaker.stop_calls)

    def test_next_moves_forward_in_candidates(self):
        items = [
            _search_item(name="晴天", sid="1"),
            _search_item(name="七里香", singer="周杰伦", sid="2"),
            _search_item(name="夜曲", singer="周杰伦", sid="3"),
        ]
        _seed_state(current=items[0], candidates=items, index=0)
        api = _ApiStub([
            ("song/url", None, lambda url, params: _envelope(
                200, {"url": f"http://cdn/{params['id']}.mp3"}
            )),
        ])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "next"})
        self.assertIn("七里香", out)
        self.assertIn("已确认出声", out)
        played = [p["url"] for p in speaker.play_calls if p["url"]]
        self.assertEqual(["http://cdn/2.mp3"], played)
        self.assertEqual("七里香", song_request._state["current"]["name"])
        self.assertEqual(1, song_request._state["index"])
        # 旧 current 进历史栈，prev 可回
        self.assertEqual("晴天", song_request._state["history"][-1]["name"])

    def test_next_wraps_around(self):
        items = [_search_item(name="晴天", sid="1"), _search_item(name="夜曲", sid="2")]
        _seed_state(current=items[1], candidates=items, index=1)
        api = _ApiStub([("song/url", None, _envelope(200, {"url": "http://cdn/{id}.mp3"}))])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "next"})
        self.assertIn("晴天", out)
        self.assertEqual(0, song_request._state["index"])

    def test_next_without_queue_falls_back_to_device(self):
        # 桥自持队列为空（原生小爱/投送在放）：回落设备自身队列
        from core.tools.registry import reply_suppressed

        speaker = FakeSpeaker(device_queue=True)
        api = _ApiStub([])
        out = self._act(api, speaker, {"action": "next"})
        self.assertEqual(1, speaker.next_calls)
        self.assertIn("设备自身播放列表", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])
        self.assertTrue(reply_suppressed())

    def test_next_without_any_queue_reports(self):
        # 设备侧也没有可切队列：明确报「没有」，且不吞本轮回复
        from core.tools.registry import reply_suppressed, reset_reply_suppression

        reset_reply_suppression()
        speaker = FakeSpeaker(device_queue=False)
        api = _ApiStub([])
        out = self._act(api, speaker, {"action": "next"})
        self.assertIn("没有可切的队列", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])
        self.assertFalse(reply_suppressed())

    def test_next_url_unavailable_reports(self):
        items = [_search_item(name="晴天", sid="1"), _search_item(name="夜曲", sid="2")]
        _seed_state(current=items[0], candidates=items, index=0)
        api = _ApiStub([("song/url", None, _envelope(404, None, msg="无资源"))])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "next"})
        self.assertIn("播放地址", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])
        # 失败不挪动队列状态
        self.assertEqual(0, song_request._state["index"])

    def test_prev_pops_history(self):
        items = [_search_item(name="晴天", sid="1"), _search_item(name="夜曲", sid="2")]
        _seed_state(current=items[1], candidates=items, index=1, history=[items[0]])
        api = _ApiStub([("song/url", None, _envelope(200, {"url": "http://cdn/{id}.mp3"}))])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "prev"})
        self.assertIn("晴天", out)
        self.assertEqual("晴天", song_request._state["current"]["name"])
        self.assertEqual([], song_request._state["history"])

    def test_prev_empty_history_steps_back_in_candidates(self):
        items = [_search_item(name="晴天", sid="1"), _search_item(name="夜曲", sid="2")]
        _seed_state(current=items[1], candidates=items, index=1)
        api = _ApiStub([("song/url", None, _envelope(200, {"url": "http://cdn/{id}.mp3"}))])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "prev"})
        self.assertIn("晴天", out)
        self.assertEqual(0, song_request._state["index"])

    def test_prev_nothing_earlier_reports(self):
        items = [_search_item(name="晴天", sid="1")]
        _seed_state(current=items[0], candidates=items, index=0)
        speaker = FakeSpeaker()
        api = _ApiStub([])
        out = self._act(api, speaker, {"action": "prev"})
        self.assertIn("没有", out)
        self.assertEqual(0, speaker.prev_calls)  # 有桥内队列时不回落设备队列
        self.assertFalse([p for p in speaker.play_calls if p["url"]])

    def test_prev_without_queue_falls_back_to_device(self):
        speaker = FakeSpeaker(device_queue=True)
        api = _ApiStub([])
        out = self._act(api, speaker, {"action": "prev"})
        self.assertEqual(1, speaker.prev_calls)
        self.assertIn("设备自身播放列表", out)


class AlbumPathTest(unittest.TestCase):
    """P6 专辑连播：/album 一次调用 → 官方曲序组队列（API-REFERENCE §4.4）。"""

    def setUp(self):
        _reset_state()
        from core.tools.registry import reset_reply_suppression

        reset_reply_suppression()

    def _act(self, api, speaker, args, **cfg):
        cfg.setdefault("album_sources", ["netease"])
        patches = _play_env(api, speaker, **cfg)
        with patches[0], patches[1], patches[2], patches[3]:
            return _run(song_request.run(args))

    def _album_env(self, items, albums=None):
        return _ApiStub([
            ("/album", "netease", _envelope(200, {
                "source": "netease",
                "name": "叶惠美",
                "artist": "周杰伦",
                "albums": albums or [
                    {"id": "18905", "name": "叶惠美", "artist": "周杰伦", "size": len(items)}
                ],
                "items": items,
                "total": len(items),
            })),
            ("song/url", None, lambda url, params: _envelope(
                200, {"url": f"http://cdn/{params['id']}.mp3", "ext": "mp3"}
            )),
        ])

    def _track(self, track_no, name, sid=None):
        item = _search_item(name=name, sid=sid or str(100 + track_no))
        item.update({"album": "叶惠美", "track_no": track_no, "source": "netease"})
        return item

    def test_album_plays_first_track_in_official_order(self):
        # 曲目乱序返回时必须按 track_no 重排，否则连播顺序错
        items = [self._track(3, "她的睫毛"), self._track(1, "以父之名"), self._track(2, "懦夫")]
        speaker = FakeSpeaker()
        out = self._act(
            self._album_env(items), speaker, {"action": "album", "query": "叶惠美"}
        )
        self.assertIn("专辑《叶惠美》", out)
        self.assertIn("第 1 首", out)
        self.assertIn("以父之名", out)
        self.assertEqual("以父之名", song_request._state["current"]["name"])
        # 队列按官方曲序向后铺：next 依次 2、3
        queue = [i["name"] for i in song_request._state["candidates"]]
        self.assertEqual(["以父之名", "懦夫", "她的睫毛"], queue)

    def test_album_artist_hint_passed_through(self):
        items = [self._track(1, "以父之名")]
        api = self._album_env(items)
        speaker = FakeSpeaker()
        self._act(
            api, speaker, {"action": "album", "query": "叶惠美", "artist": "周杰伦"}
        )
        album_calls = api.calls_for("/album")
        self.assertEqual("周杰伦", album_calls[0][1].get("artist"))
        self.assertEqual("叶惠美", album_calls[0][1].get("name"))

    def test_album_skips_unavailable_track(self):
        # 首曲取不到直链：顺延到下一首，播报与实际一致
        items = [self._track(1, "以父之名", "bad"), self._track(2, "懦夫", "ok")]
        api = _ApiStub([
            ("/album", "netease", _envelope(200, {
                "source": "netease", "name": "叶惠美", "artist": "周杰伦",
                "albums": [{"id": "18905", "name": "叶惠美", "artist": "周杰伦"}],
                "items": items, "total": 2,
            })),
            ("song/url", None, lambda url, params: _envelope(
                200,
                {"url": "http://cdn/x.mp3"} if params["id"] == "ok"
                else {"url": ""},
            )),
        ])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "album", "query": "叶惠美"})
        self.assertIn("懦夫", out)
        self.assertIn("第 2 首", out)
        self.assertEqual("懦夫", song_request._state["current"]["name"])

    def test_album_source_fallback_past_501(self):
        # 未接入 album 的源返回 501（传输层折为 None）→ 回落下一源
        items = [self._track(1, "以父之名")]
        api = _ApiStub([
            ("/album", "kuwo", None),
            ("/album", "netease", _envelope(200, {
                "source": "netease", "name": "叶惠美", "artist": "周杰伦",
                "albums": [], "items": items, "total": 1,
            })),
            ("song/url", None, lambda url, params: _envelope(
                200, {"url": "http://cdn/1.mp3"}
            )),
        ])
        speaker = FakeSpeaker()
        out = self._act(
            api,
            speaker,
            {"action": "album", "query": "叶惠美"},
            album_sources=["kuwo", "netease"],
        )
        self.assertIn("netease", out)
        self.assertIn("以父之名", out)
        album_calls = api.calls_for("/album")
        self.assertEqual(2, len(album_calls))  # kuwo 先试（501）→ netease 命中
        self.assertIn("/kuwo/album", album_calls[0][0])
        self.assertIn("/netease/album", album_calls[1][0])

    def test_album_unsupported_source_rejected(self):
        speaker = FakeSpeaker()
        out = self._act(
            _ApiStub([]), speaker, {"action": "album", "query": "叶惠美", "source": "kuwo"}
        )
        self.assertIn("不支持专辑检索", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])

    def test_album_empty_items_reports(self):
        api = _ApiStub([
            ("/album", "netease", _envelope(200, {
                "source": "netease", "name": "不存在", "artist": None,
                "albums": [], "items": [], "total": 0,
            })),
        ])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "album", "query": "不存在"})
        self.assertIn("没有找到能播放的专辑", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])

    def test_album_mismatch_refuses_and_lists_candidates(self):
        # 真机 2026-10-28：「蛋黄涅…小情歌」放出了毫不相干的 29 首日文 OST
        items = [self._track(1, "某曲")]
        items[0]["album"] = "猫小九之守护校园"
        speaker = FakeSpeaker()
        out = self._act(
            self._album_env(
                items,
                albums=[
                    {"id": "1", "name": "猫小九之守护校园", "artist": "某"},
                    {"id": "2", "name": "校园夏日小情歌", "artist": "蛋黄捏"},
                ],
            ),
            speaker,
            {"action": "album", "query": "蛋黄涅夏日校园夏日小情歌"},
        )
        self.assertIn("没找到与", out)
        self.assertIn("校园夏日小情歌", out)  # 候选回给用户/模型重试
        self.assertFalse([p for p in speaker.play_calls if p["url"]])

    def test_album_fuzzy_name_still_matches(self):
        # ASR 顺序抖动（夏日校园夏日小情歌）仍应命中《校园夏日小情歌》
        items = [self._track(1, "校园夏日小情歌")]
        items[0]["album"] = "校园夏日小情歌"
        speaker = FakeSpeaker()
        out = self._act(
            self._album_env(items), speaker, {"action": "album", "query": "夏日校园夏日小情歌"}
        )
        self.assertIn("专辑《校园夏日小情歌》", out)

    def test_album_announce_uses_actual_album_name(self):
        # 播报专辑名以曲目 album 字段为准（候选列表可能混入同名异手专辑）
        items = [self._track(1, "以父之名")]
        speaker = FakeSpeaker()
        self._act(self._album_env(items), speaker, {"action": "album", "query": "叶惠美"})
        self.assertIn("叶惠美", speaker.announce_texts[0] or "")

    def test_album_track_cross_source_fallback(self):
        # netease 对腾讯系版权（周杰伦）常 502：同曲换源补播，专辑队列不塌
        items = [self._track(1, "以父之名")]
        api = _ApiStub([
            ("/album", "netease", _envelope(200, {
                "source": "netease", "name": "叶惠美", "artist": "周杰伦",
                "albums": [{"id": "18905", "name": "叶惠美", "artist": "周杰伦"}],
                "items": items, "total": 1,
            })),
            ("song/url", "netease", _envelope(502, None, msg="upstream failure")),
            ("search", "kuwo", _envelope(200, [
                _search_item(name="以父之名", singer="周杰伦", source="kuwo", sid="228908"),
            ])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/kuwo.mp3"})),
        ])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "album", "query": "叶惠美"})
        self.assertIn("以父之名", out)
        self.assertIn("kuwo", out)
        self.assertEqual("kuwo", song_request._state["current"]["source"])
        self.assertTrue(song_request._state["album"])
        self.assertEqual(
            ["http://cdn/kuwo.mp3"], [p["url"] for p in speaker.play_calls if p["url"]]
        )

    def test_album_next_uses_cross_source_fallback(self):
        tracks = [self._track(1, "以父之名", "a1"), self._track(2, "懦夫", "a2")]
        _seed_state(current=tracks[0], candidates=tracks, index=0, album=True)
        api = _ApiStub([
            ("song/url", "netease", _envelope(502, None, msg="upstream failure")),
            ("search", "kuwo", _envelope(200, [
                _search_item(name="懦夫", singer="周杰伦", source="kuwo", sid="k2"),
            ])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/k2.mp3"})),
        ])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "next"})
        self.assertIn("懦夫", out)
        self.assertEqual(1, song_request._state["index"])
        self.assertEqual("kuwo", song_request._state["current"]["source"])

    def test_same_song_fallback_rejects_other_singer(self):
        # 同名不同手（或翻唱）不得当成本曲补播
        items = [self._track(1, "以父之名")]
        api = _ApiStub([
            ("/album", "netease", _envelope(200, {
                "source": "netease", "name": "叶惠美", "artist": "周杰伦",
                "albums": [], "items": items, "total": 1,
            })),
            ("song/url", "netease", _envelope(502, None)),
            ("search", "kuwo", _envelope(200, [
                _search_item(name="以父之名", singer="某某翻唱", source="kuwo", sid="x"),
            ])),
            ("song/url", "kuwo", _envelope(200, {"url": "http://cdn/wrong.mp3"})),
        ])
        speaker = FakeSpeaker()
        out = self._act(api, speaker, {"action": "album", "query": "叶惠美"})
        self.assertIn("没有找到能播放的专辑", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])


class AutoAdvanceTest(unittest.TestCase):
    """专辑连播：播完自动切下一首（静默、不插播报），末尾停止。

    真机 2026-10-28：首版只组队列不自动续播 ⇒ 第 1 首放完就静默停住。
    """

    def _track(self, track_no, name, sid):
        item = _search_item(name=name, sid=sid)
        item.update({"album": "叶惠美", "track_no": track_no, "source": "netease"})
        return item

    def setUp(self):
        _reset_state()
        from core.tools.registry import reset_reply_suppression

        reset_reply_suppression()

    def _api(self):
        return _ApiStub([
            ("song/url", None, lambda url, params: _envelope(
                200, {"url": f"http://cdn/{params['id']}.mp3"}
            )),
        ])

    def _run_with_end(self, outcome, scenario):
        patches = _play_env(self._api(), scenario["speaker"])
        fake_wait = mock.AsyncMock(return_value=outcome)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            mock.patch.object(song_request, "_wait_track_end", new=fake_wait),
        ):
            _run(scenario["fn"]())
        return patches

    def test_album_auto_advances_silently(self):
        t1 = self._track(1, "以父之名", "a1")
        t2 = self._track(2, "懦夫", "a2")
        speaker = FakeSpeaker()

        async def fn():
            song_request._note_played(t1, [t2], album_mode=True)
            # 真播完：起播时刻回拨到接近曲长（否则被「中断」判定拦住）
            song_request._state["started_at"] -= t1["duration_s"]
            task = song_request._advance_task
            self.assertIsNotNone(task)
            await task

        self._run_with_end("finished", {"speaker": speaker, "fn": fn})
        self.assertEqual("懦夫", song_request._state["current"]["name"])
        self.assertEqual(1, song_request._state["index"])
        self.assertEqual(
            ["http://cdn/a2.mp3"], [p["url"] for p in speaker.play_calls if p["url"]]
        )
        # 自动续播静默：没有任何 announce 文案
        self.assertEqual([], speaker.announce_texts)

    def test_interrupted_track_does_not_advance(self):
        # 唤醒提示音/桥内 TTS 会顶掉当前曲（事件面同样 idle）：不得当成播完
        t1 = self._track(1, "晴天", "a1")
        t1.update({"duration_s": 269})
        t2 = self._track(2, "懦夫", "a2")
        speaker = FakeSpeaker()

        async def fn():
            song_request._note_played(t1, [t2], album_mode=True)
            song_request._state["started_at"] -= 60  # 只放了 60s/269s
            await song_request._advance_task

        self._run_with_end("finished", {"speaker": speaker, "fn": fn})
        self.assertEqual("晴天", song_request._state["current"]["name"])
        self.assertEqual([], [p["url"] for p in speaker.play_calls if p["url"]])

    def test_last_track_arms_end_watcher_and_stops(self):
        # 最后一首也要挂监听：播完只记录「专辑播完」并停止，不切歌、不循环
        t1 = self._track(3, "她的睫毛", "a3")
        speaker = FakeSpeaker()

        async def fn():
            song_request._note_played(t1, [], album_mode=True)
            task = song_request._advance_task
            self.assertIsNotNone(task, "末尾也要挂监听以记录播完")
            song_request._state["started_at"] -= t1["duration_s"]
            await task

        self._run_with_end("finished", {"speaker": speaker, "fn": fn})
        self.assertEqual("她的睫毛", song_request._state["current"]["name"])
        self.assertEqual([], [p["url"] for p in speaker.play_calls if p["url"]])

    def test_plain_play_does_not_arm(self):
        async def fn():
            song_request._note_played(_search_item(), [_search_item(name="夜曲")])
            return song_request._advance_task

        patches = _play_env(self._api(), FakeSpeaker())
        with patches[0], patches[1], patches[2], patches[3]:
            self.assertIsNone(_run(fn()))

    def test_unreadable_playback_stops_advance(self):
        t1 = self._track(1, "以父之名", "a1")
        t2 = self._track(2, "懦夫", "a2")
        speaker = FakeSpeaker()

        async def fn():
            song_request._note_played(t1, [t2], album_mode=True)
            await song_request._advance_task

        self._run_with_end("unreadable", {"speaker": speaker, "fn": fn})
        self.assertEqual("以父之名", song_request._state["current"]["name"])
        self.assertEqual([], [p["url"] for p in speaker.play_calls if p["url"]])

    def test_weak_completion_does_not_advance(self):
        # B-03：影子/宽限兜底的完成是弱证据，不能驱动自动续播
        t1 = self._track(1, "以父之名", "a1")
        t2 = self._track(2, "懦夫", "a2")
        speaker = FakeSpeaker()

        async def fn():
            song_request._note_played(t1, [t2], album_mode=True)
            song_request._state["started_at"] -= t1["duration_s"]
            await song_request._advance_task

        self._run_with_end("weak:shadow_finished", {"speaker": speaker, "fn": fn})
        self.assertEqual("以父之名", song_request._state["current"]["name"])
        self.assertEqual([], [p["url"] for p in speaker.play_calls if p["url"]])

    def test_stop_cancels_pending_advance(self):
        t1 = self._track(1, "以父之名", "a1")
        t2 = self._track(2, "懦夫", "a2")
        speaker = FakeSpeaker()

        async def _blocking_wait(_item, _since_seq=None):
            await asyncio.sleep(3600)
            return "finished"

        async def fn():
            song_request._note_played(t1, [t2], album_mode=True)
            task = song_request._advance_task
            await song_request._do_stop()
            return task

        patches = _play_env(self._api(), speaker)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            mock.patch.object(song_request, "_wait_track_end", new=_blocking_wait),
        ):
            task = _run(fn())
        self.assertIsNone(song_request._advance_task)
        self.assertTrue(task.done() or task.cancelling())
        self.assertIsNone(song_request._state["current"])
        self.assertEqual(1, speaker.stop_calls)


class PauseResumeTest(unittest.TestCase):
    """「继续播放」不得被当成切歌（真机 2026-10-28：唤醒暂停后说继续播放被切到下一首）。"""

    def setUp(self):
        _reset_state()
        from core.tools.registry import reset_reply_suppression

        reset_reply_suppression()

    def _act(self, speaker, args):
        patches = _play_env(_ApiStub([]), speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            return _run(song_request.run(args))

    def test_resume_action_resumes_paused_playback(self):
        from core.tools.registry import reply_suppressed

        _seed_state(current=_search_item(), candidates=[_search_item()], index=0)
        speaker = FakeSpeaker(playing="paused")
        out = self._act(speaker, {"action": "resume"})
        self.assertIn("继续播放", out)
        self.assertEqual("playing", speaker.playing_states[-1])
        self.assertFalse(song_request._paused_for_wakeup)
        self.assertTrue(reply_suppressed(), "续播后必须免播，否则收尾 TTS 顶掉音乐")

    def test_resume_when_playing_is_idempotent(self):
        _seed_state(current=_search_item(), candidates=[_search_item()], index=0)
        speaker = FakeSpeaker(playing="playing")
        out = self._act(speaker, {"action": "resume"})
        self.assertIn("已经在播放", out)

    def test_resume_without_playback_reports(self):
        speaker = FakeSpeaker(playing="idle")
        out = self._act(speaker, {"action": "resume"})
        self.assertIn("没有可续播", out)

    def test_pause_action_cancels_advance_and_blocks_auto_resume(self):
        items = [_search_item(), _search_item(name="夜曲", sid="2")]
        _seed_state(current=items[0], candidates=items, index=0, album=True)
        speaker = FakeSpeaker(playing="playing")
        out = self._act(speaker, {"action": "pause"})
        self.assertIn("已暂停", out)
        self.assertEqual("paused", speaker.playing_states[-1])
        # 用户主动暂停：会话结束时不得被 resume_after_wakeup 越权恢复
        self.assertFalse(song_request._paused_for_wakeup)

        patches = _play_env(_ApiStub([]), speaker)
        with patches[0], patches[1], patches[2], patches[3]:
            resumed = _run(song_request.resume_after_wakeup("conversation_end"))
        self.assertFalse(resumed)
        self.assertEqual("paused", speaker.playing_states[-1])

    def test_validate_pause_resume_need_no_query(self):
        self.assertIsNone(song_request.validate({"action": "pause"}))
        self.assertIsNone(song_request.validate({"action": "resume"}))


class QueueHealthTest(unittest.TestCase):
    """`/api/health` 的队列观测面：只给计数/标志，不给歌名。"""

    def test_empty_and_album_flags(self):
        _reset_state()
        health = song_request.queue_health()
        self.assertFalse(health["has_queue"])
        self.assertFalse(health["album"])
        self.assertEqual(0, health["size"])
        self.assertFalse(health["auto_advance"])

        items = [_search_item(), _search_item(name="夜曲", sid="2")]
        _seed_state(current=items[0], candidates=items, index=0, album=True)
        health = song_request.queue_health()
        self.assertTrue(health["has_queue"])
        self.assertTrue(health["album"])
        self.assertEqual(2, health["size"])
        self.assertNotIn("name", str(health.keys()))  # 不泄漏曲名


class AnnounceWordListTest(unittest.TestCase):
    """要点 8：播报文案避开 KWS 词表词（announce 词表检查会拒绝）。

    词源 = deploy/config.py 真实数据面（wakeup.keywords + DIRECT/XIAOAI
    COMMANDS 键）——生产 KWS 词表就是从这份数据编译的，拿它当独立事实源。
    """

    def _deploy_config(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "deploy_config_for_song_request", ROOT / "deploy" / "config.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _wordlist(self) -> list[str]:
        deploy = self._deploy_config()
        words = [str(w) for w in deploy.APP_CONFIG["wakeup"]["keywords"] if w]
        for attr in ("DIRECT_COMMANDS", "XIAOAI_COMMANDS"):
            table = getattr(deploy, attr, None)
            if isinstance(table, dict):
                words += [str(k) for k in table if k]
        return words

    def _texts(self) -> list[str]:
        item = _search_item()
        album_item = _search_item(name="以父之名")
        album_item["album"] = "叶惠美"
        return [
            song_request._announce_text(item),
            song_request._announce_text(_search_item(name="夜曲", singer="周杰伦")),
            song_request._switch_text(item, "next"),
            song_request._switch_text(item, "prev"),
            song_request._album_announce_text("叶惠美", album_item),
            song_request._FALLBACK_ANNOUNCE,
        ]

    def test_fixed_texts_avoid_deploy_wordlist(self):
        # 子串包含口径（与 speaker.find_blocked_word 一致）：文案含词表词即违规
        for text in self._texts():
            for word in self._wordlist():
                self.assertNotIn(word, text, f"文案 {text!r} 命中词表词 {word!r}")

    def test_fixed_texts_pass_find_blocked_word(self):
        # bridge 自己的词表检查器再验一遍（根 config 词源口径）
        speaker_module = __import__(
            "tests.fake_device", fromlist=["load_speaker_module"]
        ).load_speaker_module()
        for text in self._texts():
            self.assertIsNone(
                speaker_module.find_blocked_word(text),
                f"文案 {text!r} 被 find_blocked_word 拒绝",
            )


class ConfigContractTest(unittest.TestCase):
    """要点 7：根 config.py 与 deploy/config.py 双份配置都声明 song_request 段。

    deploy 生产配置改错文件是踩过的坑——这里守门双份一致的关键缺省。
    """

    # 2026-10-28 config 合流（台账 #8 收口）：根 config = 本地开发基线保持关，
    # deploy/config.py = 生产镜像口径保持开（V1 真机验收 2026-10-27 通过后与
    # 现网对齐）——按仓库重部署不再静默关掉点歌。
    _CASES = (
        ("config.py", "config_for_song_request", False),
        ("deploy/config.py", "deploy_config_for_song_request", True),
    )

    def _load(self, rel: str, name: str):
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, ROOT / rel)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_both_configs_declare_song_request(self):
        for rel, name, expected_enabled in self._CASES:
            with self.subTest(config=rel):
                seg = self._load(rel, name).APP_CONFIG.get("song_request")
                self.assertIsInstance(seg, dict, f"{rel} 缺 song_request 段")
                # enabled 口径分侧守门：dev 基线关 / deploy 生产口径开（见 _CASES 注）
                self.assertIs(seg.get("enabled"), expected_enabled)
                self.assertTrue(str(seg.get("api_base", "")).startswith("http://"))
                # 音质固定 320k（铁律）：不暴露为可配项，代码 _QUALITY 硬编码
                self.assertNotIn("quality", seg)
                sources = [str(s).lower() for s in seg.get("sources", [])]
                self.assertNotIn("bilibili", sources)  # headers 无通道，必 403
                self.assertNotIn("migu", sources)  # 默认不收，留配置可加

    def test_exit_keywords_do_not_swallow_stop(self):
        """「停止」是媒体控制词，不能出现在对话退出词表里（否则停音乐会变成退会话
        再续播，真机 2026-10-28）。"""
        for rel, name, _enabled in self._CASES:
            module = self._load(rel, name)
            segments = [
                seg
                for seg in module.APP_CONFIG.values()
                if isinstance(seg, dict) and "exit_keywords" in seg
            ]
            self.assertTrue(segments, f"{rel} 没有任何 exit_keywords 段")
            for seg in segments:
                words = [str(w) for w in seg["exit_keywords"]]
                self.assertFalse(
                    [w for w in words if "停止" in w],
                    f"{rel} 的 exit_keywords 不得含「停止」：{words}",
                )


class WakeupPauseTest(unittest.TestCase):
    """唤醒打断：在线点歌在播时暂停（不停止），空闲后自动续播。"""

    def setUp(self):
        _reset_state()
        song_request.cancel_pending_resume()
        song_request._paused_for_wakeup = False

    def tearDown(self):
        song_request.cancel_pending_resume()
        song_request._paused_for_wakeup = False

    def test_pause_without_queue_is_noop(self):
        speaker = FakeSpeaker(playing="playing")
        with mock.patch.object(song_request, "get_speaker", return_value=speaker):
            self.assertFalse(_run(song_request.pause_for_wakeup()))
        self.assertEqual(["playing"], speaker.playing_states)

    def test_pause_then_resume(self):
        _seed_state(current=_search_item(), candidates=[_search_item()], index=0)
        speaker = FakeSpeaker(playing="playing")
        with mock.patch.object(song_request, "get_speaker", return_value=speaker):
            self.assertTrue(_run(song_request.pause_for_wakeup()))
            self.assertEqual("paused", speaker.playing_states[-1])
            self.assertTrue(_run(song_request.resume_after_wakeup("test")))
        self.assertEqual("playing", speaker.playing_states[-1])

    def test_pause_is_idempotent_after_wakeup(self):
        # 原生唤醒已暂停后，KWS/会话重置路径不得再 stop 一次
        _seed_state(current=_search_item(), candidates=[_search_item()], index=0)
        speaker = FakeSpeaker(playing="playing")
        with mock.patch.object(song_request, "get_speaker", return_value=speaker):
            self.assertTrue(_run(song_request.pause_for_wakeup()))
            self.assertTrue(_run(song_request.pause_for_wakeup()))
        self.assertEqual(["playing", "paused"], speaker.playing_states)

    def test_new_play_cancels_pending_resume(self):
        _seed_state(current=_search_item(), candidates=[_search_item()], index=0)
        speaker = FakeSpeaker(playing="playing")
        with mock.patch.object(song_request, "get_speaker", return_value=speaker):
            _run(song_request.pause_for_wakeup())
            self.assertIsNotNone(song_request._resume_task)
            song_request._note_played(_search_item(sid="2"), [_search_item(sid="2")])
            self.assertIsNone(song_request._resume_task)

    def test_auto_resume_timeout_releases_paused_flag(self):
        # 超时窗口耗尽（设备 30s 内一直没回到空闲）必须释放暂停态：
        # 否则下一轮唤醒被 is_paused_for_wakeup 短路，暂停接管语义失效
        _seed_state(current=_search_item(), candidates=[_search_item()], index=0)
        speaker = FakeSpeaker(playing="playing")

        async def scenario():
            self.assertTrue(await song_request.pause_for_wakeup())
            self.assertTrue(song_request._paused_for_wakeup)
            self.assertIsNotNone(song_request._resume_task)
            await song_request._resume_task  # MAX_S=0 → 立即超时退出

        with mock.patch.object(song_request, "get_speaker", return_value=speaker):
            with mock.patch.object(song_request, "_RESUME_MAX_S", 0.0):
                _run(scenario())
        self.assertFalse(song_request._paused_for_wakeup)

    def test_auto_resume_without_speaker_releases_paused_flag(self):
        # 拿不到 speaker 永远续不了播：不得让幂等闸门一直谎报「已接管」
        async def scenario():
            song_request._paused_for_wakeup = True
            await song_request._auto_resume(song_request._generation)
            self.assertFalse(song_request._paused_for_wakeup)

        with mock.patch.object(song_request, "get_speaker", return_value=None):
            _run(scenario())


class WaitTrackEndQualityTest(unittest.TestCase):
    """B-03：只有「主源观察到起播 + 观察到结束」才算 finished。"""

    def _track_end(self, completion, since_seq=11):
        fake = mock.Mock()
        fake.wait_for_finish = mock.AsyncMock(return_value=completion)
        with mock.patch("core.device.get_playback_events", return_value=fake):
            outcome = _run(
                song_request._wait_track_end({"duration_s": 10}, since_seq=since_seq)
            )
        return outcome, fake

    def test_shadow_finished_is_weak(self):
        from core.device.playback_events import Completion

        outcome, _ = self._track_end(
            Completion(True, True, "shadow_finished", "weak", "client")
        )
        self.assertEqual("weak:shadow_finished", outcome)

    def test_idle_grace_is_weak(self):
        from core.device.playback_events import Completion

        outcome, _ = self._track_end(
            Completion(True, False, "idle_grace", "weak", "context")
        )
        self.assertEqual("weak:idle_grace", outcome)

    def test_timeout_never_completes_even_with_readback(self):
        # 超时读回 idle 只能证明「现在空闲」，不能证明这首播完了
        from core.device.playback_events import Completion

        outcome, _ = self._track_end(
            Completion(True, True, "timeout", "weak", None)
        )
        self.assertEqual("weak:timeout", outcome)

    def test_paused_before_idle_is_not_a_finish(self):
        from core.device.playback_events import Completion

        outcome, _ = self._track_end(
            Completion(True, True, "idle", "strong", "context", paused=True)
        )
        self.assertEqual("weak:paused", outcome)

    def test_primary_idle_with_start_is_finished(self):
        from core.device.playback_events import Completion

        outcome, _ = self._track_end(Completion(True, True, "idle", "strong", "context"))
        self.assertEqual("finished", outcome)

    def test_native_finished_event_is_finished(self):
        from core.device.playback_events import Completion

        outcome, _ = self._track_end(
            Completion(True, True, "finished_event", "strong", "native")
        )
        self.assertEqual("finished", outcome)

    def test_no_observation_is_unreadable(self):
        from core.device.playback_events import Completion

        outcome, _ = self._track_end(
            Completion(False, False, "no_observation", None, None)
        )
        self.assertEqual("unreadable", outcome)

    def test_begin_playback_seq_is_forwarded(self):
        from core.device.playback_events import Completion

        _, fake = self._track_end(
            Completion(True, True, "idle", "weak", "context"), since_seq=42
        )
        self.assertEqual(42, fake.wait_for_finish.await_args.kwargs["since_seq"])


if __name__ == "__main__":
    unittest.main()
