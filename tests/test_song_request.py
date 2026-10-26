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

    def __init__(self, playing: str = "paused", start_playing: bool = True):
        self.play_calls: list[dict] = []
        self.announce_texts: list[str | None] = []
        self.stop_calls = 0
        self.playing_states: list[str] = [playing]  # get_playing 依次返回
        self.announce_error: Exception | None = None  # 只拒一次（模拟词表拒绝）
        self.start_playing = start_playing  # False 模拟 URL 源不可达（状态不翻转）

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
        self.assertIn("source", props)
        self.assertEqual(["action"], song_request.TOOL_SPEC["function"]["parameters"]["required"])


def _patch_cfg(**overrides):
    """把 song_request._cfg 换成内存表（测试不落真配置）。"""

    def _fake(key, default=None):
        return overrides.get(key, default)

    return mock.patch.object(song_request, "_cfg", side_effect=_fake)


def _reset_state():
    song_request._state.update(
        {"current": None, "history": [], "candidates": [], "index": -1}
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
        # 回落即停：kuwo 失败一跳 + netease 成功一跳，qq 不再空转
        search_urls = [u for u, _ in api.calls_for("search")]
        self.assertEqual(2, len(search_urls))
        self.assertFalse([u for u in search_urls if "qq" in u])

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
        self.assertIn("play/next/prev/stop", out)

    def test_play_without_query_rejected(self):
        with _patch_cfg(enabled=True):
            out = _run(song_request.run({"action": "play"}))
        self.assertIn("query", out)

    def test_validate_direct(self):
        self.assertIsNone(song_request.validate({"action": "stop"}))
        self.assertIsNone(song_request.validate({"action": "play", "query": "晴天"}))
        self.assertIsNotNone(song_request.validate({"action": "play"}))
        self.assertIsNotNone(song_request.validate({"action": "pause"}))
        self.assertIsNotNone(song_request.validate(None))


def _seed_state(current=None, candidates=None, index=-1, history=None):
    song_request._state.update(
        {
            "current": current,
            "candidates": candidates or [],
            "index": index,
            "history": list(history or []),
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

    def test_stop_calls_device_stop_and_clears_state(self):
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

    def test_next_without_queue_reports(self):
        speaker = FakeSpeaker()
        api = _ApiStub([])
        out = self._act(api, speaker, {"action": "next"})
        self.assertIn("没有", out)
        self.assertFalse([p for p in speaker.play_calls if p["url"]])

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
        self.assertFalse([p for p in speaker.play_calls if p["url"]])


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
        return [
            song_request._announce_text(item),
            song_request._announce_text(_search_item(name="夜曲", singer="周杰伦")),
            song_request._switch_text(item, "next"),
            song_request._switch_text(item, "prev"),
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

    _CASES = (("config.py", "config_for_song_request"), ("deploy/config.py", "deploy_config_for_song_request"))

    def _load(self, rel: str, name: str):
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, ROOT / rel)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_both_configs_declare_song_request(self):
        for rel, name in self._CASES:
            with self.subTest(config=rel):
                seg = self._load(rel, name).APP_CONFIG.get("song_request")
                self.assertIsInstance(seg, dict, f"{rel} 缺 song_request 段")
                self.assertFalse(seg.get("enabled", True))  # 默认关（真机验收窗口由用户开）
                self.assertTrue(str(seg.get("api_base", "")).startswith("http://"))
                # 音质固定 320k（铁律）：不暴露为可配项，代码 _QUALITY 硬编码
                self.assertNotIn("quality", seg)
                sources = [str(s).lower() for s in seg.get("sources", [])]
                self.assertNotIn("bilibili", sources)  # headers 无通道，必 403
                self.assertNotIn("migu", sources)  # 默认不收，留配置可加


if __name__ == "__main__":
    unittest.main()
