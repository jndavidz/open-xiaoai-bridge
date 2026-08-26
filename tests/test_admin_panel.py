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
        {"openai": {"model": "m1", "evil_key": "x"}, "hacker": {"root": True}}
    )
    assert patch == {"openai": {"model": "m1"}}, patch
    ok("白名单过滤（schema 外字段丢弃）")

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
            openai_fields = {f["path"]: f for f in sections["openai"]["fields"]}
            assert "openai.base_url" in openai_fields
            secret_field = openai_fields["openai.api_key"]
            if secret_field["value"].get("set"):
                masked = secret_field["value"]["masked"]
                real_key = str(cm.get_app_config("openai.api_key") or "")
                assert real_key not in masked and masked.startswith("*")
            ok("config GET：schema + 密钥掩码（不泄露明文）")

            r = await s.put(
                f"{base}/api/admin/config",
                headers=H,
                json={"patch": {"openai": {"model": "panel-new-model", "evil": 1}}},
            )
            assert r.status == 200
            assert cm.get_app_config("openai.model") == "panel-new-model"
            ok("config PUT：热生效（ConfigManager 立即可见新值）")

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
                json={"patch": {"openai": {"model": None}}},
            )
            assert r.status == 200
            assert cm.get_app_config("openai.model") == original_model
            ok("config PUT：null 清除覆盖，回落底层值")

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


def main():
    test_overrides()
    test_log_buffer()
    asyncio.run(test_http())
    asyncio.run(test_monitor_services())
    test_api_styles()
    asyncio.run(test_api_style_hot_reload())
    print(f"\n全部通过：{PASS} 项断言组 ✅")


if __name__ == "__main__":
    main()
