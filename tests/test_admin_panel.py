"""后台管理面板冒烟测试（不依赖音箱 / 模型 / Rust 原生扩展）。

覆盖：
    1. RuntimeOverrides：深合并、白名单过滤、持久化、null 清除回落
    2. MemoryLogHandler：seq 单调递增与增量拉取
    3. Admin API：鉴权（401）、overview 结构、config GET/PUT 热生效、
       secret 掩码、日志接口、日志级别切换、/admin 页面
    4. config/test 连通性预检：本地 mock 上游（GET /models 成功路径 +
       不可达地址失败路径）

用法（临时 venv，避免污染项目 .venv）：
    cd bridge
    uv venv /tmp/ox-admin-test-venv --python 3.12
    VIRTUAL_ENV=/tmp/ox-admin-test-venv uv pip install aiohttp requests
    /tmp/ox-admin-test-venv/bin/python tests/test_admin_panel.py
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_ROOT))

# ---- 必须先于 core.* 导入：stub 原生扩展 + 定向环境变量 ----
_fake_native = types.ModuleType("open_xiaoai_server")
for _attr in (
    "tts_stream_play",
    "tts_stream_play_background",
    "tts_play",
    "tts_play_background",
):
    setattr(_fake_native, _attr, lambda *a, **k: None)
sys.modules.setdefault("open_xiaoai_server", _fake_native)

_TMP = Path(tempfile.mkdtemp(prefix="ox-admin-test-"))
os.environ["RUNTIME_OVERRIDES_PATH"] = str(_TMP / "runtime-overrides.json")
os.environ["ADMIN_TOKEN"] = "test-token-123"
os.environ.pop("MONITOR_SERVICES", None)
os.environ.pop("OPENAI_ENABLE", None)

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from core.services.admin_api import (  # noqa: E402
    AdminAPI,
    _sanitize_patch,
    set_runtime_log_level,
)
from core.utils.config import ConfigManager  # noqa: E402
from core.utils.log_buffer import get_memory_log_handler  # noqa: E402
from core.utils.logger import logger  # noqa: E402
from core.utils.runtime_overrides import deep_merge, runtime_overrides  # noqa: E402

PASS = 0


def ok(name: str):
    global PASS
    PASS += 1
    print(f"  ✅ {name}")


# ---------------------------------------------------------------- part 1 ----
def test_overrides():
    print("[1] RuntimeOverrides")
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    deep_merge(base, {"a": {"b": 9}, "d": None, "e": 5})
    assert base == {"a": {"b": 9, "c": 2}, "e": 5}, base
    ok("deep merge + null 删除 + 新增")

    patch = _sanitize_patch(
        {"openai": {"response_timeout": 90, "model": "m1", "evil_key": "x"},
         "hacker": {"root": True}}
    )
    # model/base_url/api_key 等已随裸表单撤出白名单（防绕过预设条直接写入），
    # 只有高级区块声明的字段可通过 PUT 写覆盖层
    assert patch == {"openai": {"response_timeout": 90}}, patch
    ok("白名单过滤（model 已出白名单 + schema 外字段丢弃）")

    try:
        _sanitize_patch({"openai": {"response_timeout": "abc"}})
        raise AssertionError("int 字段应拒绝非法值")
    except ValueError:
        ok("类型校验（int 字段非法值报错）")

    runtime_overrides.update({"openai": {"model": "persist-model"}})
    saved = json.loads(Path(os.environ["RUNTIME_OVERRIDES_PATH"]).read_text())
    assert saved["openai"]["model"] == "persist-model"
    assert runtime_overrides.contains("openai.model")
    assert not runtime_overrides.contains("openai.api_key")
    ok("原子持久化 + contains 来源标注")

    # 恢复干净状态，避免影响后续 HTTP 用例的基线
    runtime_overrides.save({})
    ConfigManager.instance().reload_app_config()
    ok("清理覆盖层复位")


# ---------------------------------------------------------------- part 2 ----
def test_log_buffer():
    print("[2] MemoryLogHandler")
    handler = get_memory_log_handler()
    logger.info("hello-admin-panel")
    entries, latest = handler.get_after(after=-1)
    assert any("hello-admin-panel" in e["msg"] for e in entries)
    seq0 = handler.latest_seq()
    logger.warning("warn-entry-2")
    entries2, latest2 = handler.get_after(after=seq0)
    assert len(entries2) == 1 and entries2[0]["level"] == "WARNING"
    assert latest2 == seq0 + 1
    ok("写入 + 按 seq 增量拉取")


# ---------------------------------------------------------------- part 3 ----
async def test_http():
    print("[3] Admin API over HTTP")
    cm = ConfigManager.instance()
    original_model = cm.get_app_config("openai.model")

    admin = AdminAPI(static_dir=str(BRIDGE_ROOT / "core/services/admin_static"))
    app = web.Application(middlewares=[admin.auth_middleware])
    admin.register(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18923)
    await site.start()

    # mock 上游（成功路径）：GET /v1/models -> 200；并严格校验探测头携带
    # 的正是本次提交的待测 Key（回归：预检不得复用运行时旧 Key）
    async def models_handler(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Bearer k-test":
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response({"data": []})

    upstream = web.Application()
    upstream.router.add_get("/v1/models", models_handler)

    up_runner = web.AppRunner(upstream)
    await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 18924)
    await up_site.start()

    H = {"Authorization": "Bearer test-token-123"}
    base = "http://127.0.0.1:18923"

    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{base}/api/admin/overview")
            assert r.status == 401
            ok("缺 token -> 401")

            r = await s.get(
                f"{base}/api/admin/overview", headers={"Authorization": "Bearer wrong"}
            )
            assert r.status == 401
            ok("错误 token -> 401")

            r = await s.get(f"{base}/api/admin/overview", headers=H)
            assert r.status == 200
            data = (await r.json())["data"]
            for key in ("app", "speaker", "backends", "audio", "external_services", "runtime"):
                assert key in data, key
            assert "openai" in data["backends"]
            assert data["external_services"] == []  # MONITOR_SERVICES 未配置
            ok("overview 结构完整（含外部服务段）")

            r = await s.get(f"{base}/api/admin/config", headers=H)
            body = await r.json()
            assert body["success"]
            schema = body["data"]["schema"]
            sections = {sec["id"]: sec for sec in schema}
            # 裸表单已撤：地址/规格/模型/Key 不再开放编辑，只保留高级兜底项
            assert "openai" not in sections, "openai 裸表单应已删除"
            adv_fields = {f["path"]: f for f in sections["openai_advanced"]["fields"]}
            assert "openai.response_timeout" in adv_fields
            assert "openai.base_url" not in adv_fields and "openai.api_key" not in adv_fields
            ok("config GET：openai 裸表单撤除，仅剩高级区块（response_timeout）")

            r = await s.put(
                f"{base}/api/admin/config",
                headers=H,
                json={"patch": {"openai": {"response_timeout": "not-int"}}},
            )
            assert r.status == 400
            ok("config PUT：非法值 -> 400")

            r = await s.put(
                f"{base}/api/admin/config",
                headers=H,
                json={"patch": {"openai": {"response_timeout": 60}}},
            )
            assert r.status == 200
            assert cm.get_app_config("openai.response_timeout") == 60
            # 高级区块之外的 openai 字段已出白名单：PUT 不再接受（防绕过预设条）
            r = await s.put(
                f"{base}/api/admin/config",
                headers=H,
                json={"patch": {"openai": {"model": "bypass-model"}}},
            )
            assert r.status == 200
            assert cm.get_app_config("openai.model") != "bypass-model", "白名单外字段必须被丢弃"
            ok("config PUT：高级字段可写；地址/规格/模型/Key 已出白名单（防绕过预设条）")

            logger.info("log-endpoint-probe")
            r = await s.get(f"{base}/api/admin/logs?after=-1", headers=H)
            d = (await r.json())["data"]
            assert d["latest_seq"] >= 1 and d["entries"]
            tail_seq = d["entries"][-1]["seq"]
            r = await s.get(f"{base}/api/admin/logs?after={tail_seq}", headers=H)
            assert (await r.json())["data"]["entries"] == []
            ok("logs 增量语义（after 之后为空）")

            set_runtime_log_level("DEBUG", __import__("logging").DEBUG)
            r = await s.post(
                f"{base}/api/admin/logs/level", headers=H, json={"level": "INFO"}
            )
            assert r.status == 200
            import logging

            assert logging.getLogger("xiaozhi").getEffectiveLevel() == logging.INFO
            ok("日志级别运行时切换")

            r = await s.get(f"{base}/admin")
            text = await r.text()
            assert r.status == 200 and "桥接控制台" in text
            ok("/admin 页面可达")

            r = await s.post(
                f"{base}/api/admin/config/test",
                headers=H,
                json={
                    "backend": "openai",
                    "base_url": "http://127.0.0.1:18924/v1",
                    "api_key": "k-test",
                    "model": "m-test",
                },
            )
            d = (await r.json())["data"]
            assert d["ok"] and d["via"].startswith("GET /models") and d["style"] == "chat_completions", d
            ok("连通性预检：mock 上游 GET /models 通过")

            # responses 规格的预检（style 透传 + GET /models 探测）
            r = await s.post(
                f"{base}/api/admin/config/test",
                headers=H,
                json={
                    "backend": "openai",
                    "base_url": "http://127.0.0.1:18924/v1",
                    "api_key": "k-test",
                    "model": "m-test",
                    "style": "openai_responses",
                },
            )
            d = (await r.json())["data"]
            assert d["ok"] and d["style"] == "openai_responses" and d["via"].startswith("GET /models"), d
            ok("连通性预检：openai_responses 规格透传")

            r = await s.post(
                f"{base}/api/admin/config/test",
                headers=H,
                json={"backend": "openai", "base_url": "http://127.0.0.1:9/v1"},
            )
            d = (await r.json())["data"]
            assert not d["ok"], d
            ok("连通性预检：不可达地址正确报告失败")
    finally:
        await runner.cleanup()
        await up_runner.cleanup()


async def test_monitor_services():
    print("[4] MONITOR_SERVICES 外部服务探测")
    # 本用例自带探针目标（上一用例的 mock 已随 runner 清理）
    upstream = web.Application()
    upstream.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    up_runner = web.AppRunner(upstream)
    await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 18926)
    await up_site.start()

    os.environ["MONITOR_SERVICES"] = json.dumps(
        [
            {"name": "mock-upstream", "url": "http://127.0.0.1:18926/v1/models", "auth": "Bearer david"},
            {"name": "blackhole", "url": "http://127.0.0.1:9/v1/models"},
        ]
    )
    try:
        admin = AdminAPI(static_dir=str(BRIDGE_ROOT / "core/services/admin_static"))
        results = await admin._probe_external_services()
        by_name = {r["name"]: r for r in results}
        assert by_name["mock-upstream"]["ok"] and by_name["mock-upstream"]["status"] == 200
        assert not by_name["blackhole"]["ok"] and by_name["blackhole"]["error"]
        ok("MONITOR_SERVICES 外部服务并发探测（在线/离线各一）")
    finally:
        os.environ.pop("MONITOR_SERVICES", None)
        await up_runner.cleanup()


# ---------------------------------------------------------------- part 5 ----
def test_api_styles():
    print("[5] 接口规格（api_style）请求构造与响应解析")
    from core.openai import OpenAIManager

    messages = [
        {"role": "system", "content": "你是贾维斯"},
        {"role": "user", "content": "你好"},
    ]

    OpenAIManager._base_url = "https://gw.example/v1"
    OpenAIManager._model = "m-test"
    OpenAIManager._max_tokens = 300
    OpenAIManager._temperature = 0.5
    OpenAIManager._api_key = "sk-test"
    original_style = OpenAIManager._api_style

    try:
        url, payload, headers = OpenAIManager._build_chat_completions_request(messages)
        assert url.endswith("/chat/completions") and payload["messages"] is messages
        assert headers.get("X-Hermes-Session-Key")

        OpenAIManager._api_style = "openai_responses"
        url2, payload2, h2 = OpenAIManager._build_responses_request(messages)
        assert url2.endswith("/responses")
        assert payload2["instructions"] == "你是贾维斯"
        assert payload2["input"] == [{"role": "user", "content": "你好"}]
        assert payload2["max_output_tokens"] == 300 and "messages" not in payload2
        assert "X-Hermes-Session-Key" not in h2

        OpenAIManager._api_style = "anthropic_messages"
        url3, payload3, h3 = OpenAIManager._build_anthropic_request(messages)
        assert url3.endswith("/messages")
        assert payload3["system"] == "你是贾维斯"
        assert payload3["max_tokens"] == 300
        assert h3.get("x-api-key") == "sk-test" and h3.get("anthropic-version")

        assert OpenAIManager._extract_responses_text({"output_text": "聚合答案"}) == "聚合答案"
        assert OpenAIManager._extract_responses_text(
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": "分"}]}]}
        ) == "分"
        assert OpenAIManager._extract_anthropic_text(
            {"content": [{"type": "text", "text": "Anthropic 答案"}, {"type": "other"}]}
        ) == "Anthropic 答案"
        ok("三种规格的端点/载荷/鉴权头构造 + 响应文本提取")
    finally:
        OpenAIManager._api_style = original_style


async def test_api_style_hot_reload():
    print("[6] api_style 经面板写入热生效")
    from core.openai import OpenAIManager

    cm = ConfigManager.instance()
    original = OpenAIManager._api_style
    runtime_overrides.update({"openai": {"api_style": "openai_responses"}})
    cm.reload_app_config()
    assert OpenAIManager._api_style == "openai_responses"
    runtime_overrides.update({"openai": {"api_style": None}})
    cm.reload_app_config()
    assert OpenAIManager._api_style == original
    ok("api_style 覆盖写入 → Manager 类变量热刷新")


def test_presets_unit():
    print("[7] BackendPresets 预设库（单元）")
    import core.utils.backend_presets as bpmod
    from core.utils.backend_presets import BackendPresets, normalize_preset

    tmp = Path(tempfile.mkdtemp(prefix="ox-presets-unit-"))

    # 1) 归一化：脏数据不应让面板打不开
    p = normalize_preset({"name": "  ", "base_url": "https://x/v1", "api_style": "bogus"})
    assert p["name"] == "https://x/v1", p          # 空名回落为 base_url
    assert p["api_style"] == "chat_completions", p  # 非法规格回落默认
    assert p["id"] and p["id"] != "bogus"
    ok("normalize：脏数据规整（空名/非法规格回落）")

    # 2) 掩码：列表默认不泄露明文 key
    store = BackendPresets(tmp / "presets.json")
    added = store.add({"name": "A", "base_url": "https://a/v1", "model": "ma",
                       "api_key": "sk-super-secret-1234"})
    assert added["api_key"]["set"] and "1234" in added["api_key"]["masked"]
    assert added["api_key"]["masked"].startswith("*")
    assert "sk-super-secret-1234" not in json.dumps(store.all())
    assert store.get(added["id"], mask=False)["api_key"] == "sk-super-secret-1234"
    ok("掩码：列表/读取默认掩码，get_raw 取明文")

    # 3) ID 冲突自动重分配，不覆盖既有预设
    b = store.add({"id": added["id"], "name": "B", "base_url": "https://b/v1",
                   "model": "mb", "api_key": ""})
    assert b["id"] != added["id"]
    assert len(store.all()) == 2
    ok("新增：ID 冲突自动重分配（不覆盖既有项）")

    # 4) 持久化 + 重载
    reloaded = BackendPresets(tmp / "presets.json")
    assert len(reloaded.all()) == 2
    assert reloaded.get(added["id"], mask=False)["api_key"] == "sk-super-secret-1234"
    ok("持久化：落盘后重新加载内容一致")

    # 5) 更新：ID 不可被外部改写；留空 key 保持不变
    upd = reloaded.update(added["id"], {"id": "hacked", "name": "A2",
                                        "model": "ma2", "api_key": ""})
    assert upd["id"] == added["id"] and upd["name"] == "A2"
    assert reloaded.get(added["id"], mask=False)["api_key"] == "sk-super-secret-1234"
    ok("更新：ID 不可变 + api_key 留空保持原值")

    # 6) 删除
    assert reloaded.delete(b["id"]) is True
    assert reloaded.delete(b["id"]) is False  # 重复删除返回 False
    assert len(reloaded.all()) == 1
    ok("删除：幂等，重复删除返回 False")

    # 7) 重排：未列出的项保持相对顺序追加到末尾（不丢数据）
    for i, nm in enumerate(["c", "d", "e"]):
        reloaded.add({"name": nm, "base_url": f"https://{nm}/v1", "model": "m", "api_key": ""})
    ids = [p["id"] for p in reloaded.all()]
    reordered = reloaded.reorder([ids[3], ids[1]])
    got = [p["id"] for p in reordered]
    assert got[:2] == [ids[3], ids[1]], got
    assert sorted(got) == sorted(ids), got  # 一项都不能丢
    ok("重排：未列出的项追加末尾，不丢数据")

    # 8) to_patch：缺失字段不写 null（null 在覆盖层 = 删除回落）
    patch = BackendPresets.to_patch({"base_url": "https://z/v1", "model": "mz"})
    assert patch == {"openai": {"base_url": "https://z/v1", "model": "mz"}}, patch
    assert BackendPresets.to_patch({}) == {}
    ok("to_patch：缺失字段不写 null（避免误清配置）")

    # 9) 损坏文件不阻断启动
    (tmp / "broken.json").write_text("{not json", encoding="utf-8")
    assert BackendPresets(tmp / "broken.json").all() == []
    ok("损坏文件：降级为空列表不抛异常")

    # 10) 与 admin_api 的规格常量保持一致（防止两处漂移）
    from core.openai import API_STYLES as OPENAI_STYLES

    assert set(bpmod.API_STYLES) == set(OPENAI_STYLES)
    ok("规格常量与 core.openai.API_STYLES 一致")


async def test_presets_http():
    print("[8] 预设 API + 一键切换 + 测速（HTTP）")
    import core.utils.backend_presets as bpmod
    from core.utils.backend_presets import BackendPresets

    cm = ConfigManager.instance()
    # 记录底层（config.py/环境变量）原始值：清除覆盖后应回落到这里
    original_base = cm.get_app_config("openai.base_url")
    tmp = Path(tempfile.mkdtemp(prefix="ox-presets-http-"))
    store = BackendPresets(tmp / "presets.json")

    # mock 上游：快的 20ms / 慢的 200ms；均返回 usage 以便校验生成速度口径
    async def chat_fast(request: web.Request) -> web.Response:
        await asyncio.sleep(0.02)
        return web.json_response({
            "choices": [{"message": {"content": "我是快速模型，可以帮你答疑和编排设备。"}}],
            "usage": {"completion_tokens": 30},
        })

    async def chat_slow(request: web.Request) -> web.Response:
        await asyncio.sleep(0.2)
        return web.json_response({
            "choices": [{"message": {"content": "我是慢速模型。"}}],
            "usage": {"completion_tokens": 20},
        })

    async def chat_boom(request: web.Request) -> web.Response:
        return web.json_response({"error": {"message": "invalid api key"}}, status=401)

    upstream = web.Application()
    upstream.router.add_post("/fast/v1/chat/completions", chat_fast)
    upstream.router.add_post("/slow/v1/chat/completions", chat_slow)
    upstream.router.add_post("/boom/v1/chat/completions", chat_boom)
    up_runner = web.AppRunner(upstream)
    await up_runner.setup()
    await web.TCPSite(up_runner, "127.0.0.1", 18927).start()

    admin = AdminAPI(static_dir=str(BRIDGE_ROOT / "core/services/admin_static"), presets=store)
    app = web.Application(middlewares=[admin.auth_middleware])
    admin.register(app)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18928).start()

    H = {"Authorization": "Bearer test-token-123"}
    base = "http://127.0.0.1:18928"
    U = "http://127.0.0.1:18927"

    try:
        async with aiohttp.ClientSession() as s:
            # ---- 鉴权 ----
            r = await s.get(f"{base}/api/admin/presets")
            assert r.status == 401, r.status
            ok("预设 API：缺 token -> 401")

            # ---- 空列表 ----
            r = await s.get(f"{base}/api/admin/presets", headers=H)
            d = (await r.json())["data"]
            assert d["presets"] == [] and d["styles"], d
            ok("预设 API：初始为空 + 返回可选规格")

            # ---- 新增两条（通用模板） + 一条必定失败的 ----
            specs = [("fast", "快速模型", "/fast/v1"), ("slow", "慢速模型", "/slow/v1"),
                     ("boom", "故障模型", "/boom/v1")]
            created = {}
            for tag, name, path in specs:
                r = await s.post(f"{base}/api/admin/presets", headers=H, json={
                    "name": name, "base_url": U + path,
                    "api_style": "chat_completions", "model": f"m-{tag}",
                    "api_key": "k-bench",
                })
                assert r.status == 200, await r.text()
                created[tag] = (await r.json())["data"]["preset"]

            r = await s.get(f"{base}/api/admin/presets", headers=H)
            d = (await r.json())["data"]
            assert len(d["presets"]) == 3
            # 列表必须掩码：明文 key 不得出现在响应里
            assert "k-bench" not in json.dumps(d)
            assert d["presets"][0]["api_key"]["set"] is True
            ok("新增预设：通用模板 + 列表掩码（不泄露明文 key）")

            # ---- 编辑（改名） ----
            r = await s.put(f"{base}/api/admin/presets/{created['slow']['id']}", headers=H,
                            json={"name": "慢速模型2", "base_url": U + "/slow/v1",
                                  "api_style": "chat_completions", "model": "m-slow"})
            assert r.status == 200
            assert (await r.json())["data"]["preset"]["name"] == "慢速模型2"
            r = await s.put(f"{base}/api/admin/presets/nope", headers=H, json={"name": "x"})
            assert r.status == 404
            ok("编辑预设：改名生效 + 不存在的 ID -> 404")

            # ---- 一键切换：写入覆盖层并热生效 ----
            r = await s.post(f"{base}/api/admin/presets/{created['fast']['id']}/switch", headers=H)
            d = (await r.json())["data"]
            assert cm.get_app_config("openai.base_url") == U + "/fast/v1", d
            assert cm.get_app_config("openai.model") == "m-fast"
            assert cm.get_app_config("openai.api_key") == "k-bench"
            assert set(d["applied"]) == {"openai.base_url", "openai.api_style",
                                        "openai.model", "openai.api_key"}
            ok("一键切换：预设写入覆盖层并热生效（ConfigManager 立即可见）")

            # 切到另一条，验证是真的切换而非累加
            r = await s.post(f"{base}/api/admin/presets/{created['slow']['id']}/switch", headers=H)
            assert cm.get_app_config("openai.base_url") == U + "/slow/v1"
            assert cm.get_app_config("openai.model") == "m-slow"
            ok("一键切换：二次切换覆盖前值（非累加）")

            # 不存在的预设 -> 404
            r = await s.post(f"{base}/api/admin/presets/nope/switch", headers=H)
            assert r.status == 404

            # ---- 测速：按耗时升序 ----
            r = await s.post(f"{base}/api/admin/presets/benchmark", headers=H,
                             json={"rounds": 2, "max_tokens": 64, "prompt": "你好"})
            d = (await r.json())["data"]
            results = d["results"]
            assert len(results) == 3, results
            by_name = {x["name"]: x for x in results}

            fast, slow, boom = by_name["快速模型"], by_name["慢速模型2"], by_name["故障模型"]
            assert fast["ok"] and slow["ok"] and not boom["ok"], results
            assert fast["avg_ms"] < slow["avg_ms"], (fast["avg_ms"], slow["avg_ms"])
            # rounds=2 → 首轮预热不计入，统计轮数 = 1
            assert fast["total_rounds"] == 2 and fast["rounds"] == 1
            assert fast["ok_rounds"] == 1 and fast["min_ms"] <= fast["avg_ms"]
            # 生成速度口径：usage.completion_tokens 被取到
            assert fast["tokens"] == 30 and fast["chars"] > 0, fast
            assert boom["error"] and "401" in boom["error"], boom
            # 排序：快的在最前，失败项垫底
            assert [x["name"] for x in results] == ["快速模型", "慢速模型2", "故障模型"], results
            ok("测速：按平均耗时升序 + 失败项垫底 + tokens/耗时统计")

            # ---- 测速范围：仅测指定 ID ----
            r = await s.post(f"{base}/api/admin/presets/benchmark", headers=H,
                             json={"ids": [created["slow"]["id"]], "rounds": 1})
            d = (await r.json())["data"]
            assert len(d["results"]) == 1 and d["results"][0]["name"] == "慢速模型2", d
            ok("测速范围：ids 可限定候选")

            # ids 传不存在的 ID -> 空结果不报错
            r = await s.post(f"{base}/api/admin/presets/benchmark", headers=H,
                             json={"ids": ["ghost"], "rounds": 1})
            assert (await r.json())["data"]["results"] == []
            ok("测速范围：未知 ID 返回空结果（不 500）")

            # ---- 非法参数：rounds 非数字不应 500 ----
            r = await s.post(f"{base}/api/admin/presets/benchmark", headers=H,
                             json={"ids": [created["fast"]["id"]], "rounds": "abc"})
            assert r.status == 200, await r.text()
            ok("测速：非法 rounds 回落默认值（不 500）")

            # ---- 删除 ----
            r = await s.delete(f"{base}/api/admin/presets/{created['boom']['id']}", headers=H)
            assert r.status == 200
            r = await s.delete(f"{base}/api/admin/presets/{created['boom']['id']}", headers=H)
            assert r.status == 404
            r = await s.get(f"{base}/api/admin/presets", headers=H)
            assert len((await r.json())["data"]["presets"]) == 2
            ok("删除预设：幂等，重复删除 -> 404")

            # 删除后不应影响已生效配置（预设只是按钮）
            assert cm.get_app_config("openai.base_url") == U + "/slow/v1"
            ok("删除预设不影响已生效配置（预设库与覆盖层分离）")

            # ---- 重排 ----
            r = await s.put(f"{base}/api/admin/presets", headers=H,
                            json={"ids": [created["slow"]["id"], created["fast"]["id"]]})
            d = (await r.json())["data"]
            assert [p["id"] for p in d["presets"]] == [created["slow"]["id"], created["fast"]["id"]]
            r = await s.put(f"{base}/api/admin/presets", headers=H, json={"name": "x"})
            assert r.status == 400
            ok("重排：顺序生效 + 缺 ids -> 400")

            # ---- 复制现有按钮：copy_from 服务端取明文，Key 一并带走 ----
            r = await s.post(f"{base}/api/admin/presets", headers=H,
                             json={"copy_from": created["fast"]["id"], "name": "快速副本"})
            d = (await r.json())["data"]["preset"]
            assert d["name"] == "快速副本"
            assert d["base_url"] == U + "/fast/v1" and d["model"] == "m-fast", d
            # 掩码视图应与源一致（Key 被复制，而非清空）
            assert d["api_key"]["set"] and d["api_key"]["masked"] != "", d
            r = await s.post(f"{base}/api/admin/presets", headers=H,
                             json={"copy_from": "ghost", "name": "x"})
            assert r.status == 404
            ok("复制预设：copy_from 服务端复制（Key 一并带走）+ 源不存在 -> 404")

            # ---- 清除覆盖：回落 config.py/环境变量原始值 ----
            # 先制造覆盖（前面 switch 已写入），确认清除前后的差异
            assert cm.get_app_config("openai.base_url") == U + "/slow/v1"
            r = await s.post(f"{base}/api/admin/presets/clear-overrides", headers=H)
            d = (await r.json())["data"]
            assert set(d["cleared"]) >= {"openai.base_url", "openai.model",
                                        "openai.api_style", "openai.api_key"}, d
            # 清除后应回落底层值（config.py 的 DeepSeek 官方地址，而非预设值）
            assert cm.get_app_config("openai.base_url") != U + "/slow/v1", d["values"]
            assert cm.get_app_config("openai.base_url") == original_base
            ok("清除覆盖：地址/规格/模型/Key 全部回落底层原始值（预设不受影响）")

            # 幂等：再清一次应报 cleared=[] 且不 500
            r = await s.post(f"{base}/api/admin/presets/clear-overrides", headers=H)
            assert (await r.json())["data"]["cleared"] == []
            ok("清除覆盖：幂等（无覆盖时 cleared 为空）")

            # 预设列表仍在（清除覆盖 ≠ 删预设）
            r = await s.get(f"{base}/api/admin/presets", headers=H)
            assert len((await r.json())["data"]["presets"]) == 3
            ok("清除覆盖：预设库原样保留")
    finally:
        await runner.cleanup()
        await up_runner.cleanup()
        # 还原本用例写入的覆盖层，避免污染后续用例
        runtime_overrides.save({})
        cm.reload_app_config()


def main():
    test_overrides()
    test_log_buffer()
    asyncio.run(test_http())
    asyncio.run(test_monitor_services())
    test_api_styles()
    asyncio.run(test_api_style_hot_reload())
    test_presets_unit()
    asyncio.run(test_presets_http())
    print(f"\n全部通过：{PASS} 项断言组 ✅")


if __name__ == "__main__":
    main()
