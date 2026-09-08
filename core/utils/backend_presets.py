"""AI 对话后端预设库（一键切换的按钮配置）。

定位：与 runtime-overrides 严格分离的「候选配置库」。

    - `data/runtime-overrides.json` = **当前生效值**（面板值 > config.py/env）
    - `data/backend-presets.json`   = **可切换的候选按钮**（本模块）

两者不合并：预设只是按钮，点「切换」时才把它的字段写进覆盖层生效。
因此预设可以放心增删、排序、改名，绝不会误改线上生效配置。

持久化路径同 runtime_overrides 的约定：
    - 默认值 <bridge>/data/backend-presets.json
    - 容器部署经 BACKEND_PRESETS_PATH 指向持久化卷（如 /app/data/...），
      需要在 docker-compose.yml 里与 RUNTIME_OVERRIDES_PATH 一并挂载 ./data。

预设结构（每条）：
    {
      "id": "ds-chat",                 // 稳定唯一 ID（前端 data-id / 删除定位）
      "name": "DeepSeek",              // 按钮显示名
      "base_url": "https://api.deepseek.com/v1",
      "api_style": "chat_completions", // chat_completions | openai_responses | anthropic_messages
      "model": "deepseek-chat",
      "api_key": "sk-..."              // 明文保存；读取时可 mask=true 掩码
    }

api_key 安全说明：
    明文落盘与 config.py / .env 的既有做法同级（容器卷 + 局域网 + ADMIN_TOKEN 鉴权）。
    API 读取默认返回掩码（mask=true），仅在「复制预设」等需要回填表单的场景返回明文。
"""

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator, Sequence

from core.utils.logger import logger

# 与 core.openai.API_STYLES / admin_api.CONFIG_SCHEMA 的 options 保持一致
API_STYLES = ("chat_completions", "openai_responses", "anthropic_messages")

# 预设里的可编辑字段（与 admin_api 的 openai section 字段一一对应）
PRESET_FIELDS = ("base_url", "api_style", "model", "api_key")

# 点分路径 -> 预设短名（供 apply/switch 展开为覆盖层 patch）
FIELD_PATHS = {
    "base_url": "openai.base_url",
    "api_style": "openai.api_style",
    "model": "openai.model",
    "api_key": "openai.api_key",
}

_SECRET_MASK_TAIL = 4
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _default_presets_path() -> Path:
    """默认预设文件路径：<bridge>/data/backend-presets.json。"""
    env_path = os.environ.get("BACKEND_PRESETS_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    # core/utils/backend_presets.py -> parents[1] == bridge/
    return Path(__file__).resolve().parents[1] / "data" / "backend-presets.json"


def mask_secret(value: Any) -> dict[str, Any]:
    """密钥掩码视图：只暴露是否配置、长度与尾 4 位。"""
    text = str(value or "")
    if not text:
        return {"set": False, "masked": ""}
    if len(text) <= _SECRET_MASK_TAIL:
        masked = "***"
    else:
        masked = "*" * min(12, len(text) - _SECRET_MASK_TAIL) + text[-_SECRET_MASK_TAIL:]
    return {"set": True, "masked": masked}


def normalize_preset(raw: Any, *, default_id: str | None = None) -> dict[str, Any]:
    """把任意输入规整为合法预设字典；非法字段按类型强制转换或回落默认。

    不抛异常——预设库是用户数据，单条脏数据不应让整个面板打不开；
    无法规整的字段回落为空值，由调用方（测试连接/切换）再报错。
    """
    src = raw if isinstance(raw, dict) else {}

    name = str(src.get("name") or "").strip()
    if not name:
        name = str(src.get("base_url") or "未命名预设").strip()[:40]

    pid = str(src.get("id") or "").strip()
    if not _ID_PATTERN.match(pid):
        pid = default_id or uuid.uuid4().hex[:12]

    api_style = str(src.get("api_style") or "").strip()
    if api_style not in API_STYLES:
        api_style = "chat_completions"

    preset: dict[str, Any] = {
        "id": pid,
        "name": name,
        "base_url": str(src.get("base_url") or "").strip(),
        "api_style": api_style,
        "model": str(src.get("model") or "").strip(),
        # api_key 允许为空（例如本地 ollama / LM Studio 不需要鉴权）
        "api_key": str(src.get("api_key") or ""),
    }
    # 保留未知字段，避免未来扩展（如 temperature）被静默丢弃
    for key, value in src.items():
        if key not in preset:
            preset[key] = value
    return preset


class BackendPresets:
    """AI 对话后端预设库（单例由模块级 ``backend_presets`` 提供）。"""

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else _default_presets_path()
        self._lock = threading.RLock()
        self._presets: list[dict[str, Any]] = []
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    # ---- persistence ----

    def load(self) -> list[dict[str, Any]]:
        """从磁盘加载预设（文件缺失/损坏时视为空列表并保留现场）。"""
        with self._lock:
            try:
                raw = self._path.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else []
                items = data if isinstance(data, list) else []
            except FileNotFoundError:
                items = []
            except Exception as exc:  # 损坏不静默：打日志但不阻断启动
                logger.error(
                    f"[BackendPresets] Failed to load {self._path}: "
                    f"{type(exc).__name__}: {exc}"
                )
                items = []
            self._presets = [normalize_preset(item) for item in items if isinstance(item, dict)]
            return self._presets

    def save(self, presets: list[dict[str, Any]]) -> None:
        """原子写盘（tmp + rename），目录不存在则创建。"""
        with self._lock:
            self._presets = presets
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(presets, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self._path)

    # ---- read ----

    def all(self, *, mask: bool = True) -> list[dict[str, Any]]:
        """返回预设列表副本；mask=True 时 api_key 换成掩码视图。"""
        with self._lock:
            items = json.loads(json.dumps(self._presets, ensure_ascii=False))
        if not mask:
            return items
        for item in items:
            item["api_key"] = mask_secret(item.get("api_key"))
        return items

    def get(self, preset_id: str, *, mask: bool = True) -> dict[str, Any] | None:
        """按 ID 取单条预设；未找到返回 None。"""
        with self._lock:
            found = next((p for p in self._presets if p.get("id") == preset_id), None)
            if found is None:
                return None
            item = json.loads(json.dumps(found, ensure_ascii=False))
        if mask:
            item["api_key"] = mask_secret(item.get("api_key"))
        return item

    def get_raw(self, preset_id: str) -> dict[str, Any] | None:
        """按 ID 取单条预设的明文副本（含 api_key），仅供服务端内部使用。"""
        return self.get(preset_id, mask=False)

    # ---- write ----

    def add(self, raw: dict[str, Any]) -> dict[str, Any]:
        """新增预设（ID 冲突时自动重新生成），返回掩码视图。"""
        with self._lock:
            existing_ids = {p.get("id") for p in self._presets}
            preset = normalize_preset(raw)
            if preset["id"] in existing_ids:
                preset["id"] = uuid.uuid4().hex[:12]
            self._presets.append(preset)
            self.save(self._presets)
        return self.get(preset["id"])  # type: ignore[return-value]

    def update(self, preset_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
        """整体更新一条预设（ID 本身不可改），返回掩码视图或 None。

        api_key 三态（与面板「留空=保持不变」的语义一致）：
            - 键不存在 或 值为 ""  → 保留已保存的原值（前端回传的是掩码，拿不到明文）
            - 值为 null            → 显式清除（改回无鉴权服务）
            - 其它非空字符串        → 更新为新的 Key
        """
        with self._lock:
            index = next(
                (i for i, p in enumerate(self._presets) if p.get("id") == preset_id), None
            )
            if index is None:
                return None
            previous = self._presets[index]

            # 先按原值归一化，再单独裁决 api_key（normalize 会把缺失/空统一成 ""）
            updated = normalize_preset({**previous, **(raw if isinstance(raw, dict) else {})},
                                       default_id=preset_id)
            updated["id"] = preset_id  # ID 不可变，防止外部改写导致引用失效

            raw_dict = raw if isinstance(raw, dict) else {}
            if "api_key" in raw_dict:
                incoming = raw_dict["api_key"]
                if incoming is None:
                    updated["api_key"] = ""                      # 显式清除
                elif str(incoming).strip() == "":
                    updated["api_key"] = str(previous.get("api_key") or "")  # 保留原值
                else:
                    updated["api_key"] = str(incoming)
            else:
                updated["api_key"] = str(previous.get("api_key") or "")

            self._presets[index] = updated
            self.save(self._presets)
        return self.get(preset_id)

    def delete(self, preset_id: str) -> bool:
        """删除一条预设，返回是否命中。"""
        with self._lock:
            remaining = [p for p in self._presets if p.get("id") != preset_id]
            if len(remaining) == len(self._presets):
                return False
            self.save(remaining)
            return True

    def reorder(self, ordered_ids: Sequence[str]) -> list[dict[str, Any]]:
        """按给定 ID 顺序重排；未列出的项保持原相对顺序追加到末尾。"""
        with self._lock:
            by_id = {p.get("id"): p for p in self._presets}
            ordered = [by_id.pop(pid) for pid in ordered_ids if pid in by_id]
            ordered.extend(by_id.values())  # 兜底：未列出的项不丢
            self.save(ordered)
        return self.all()

    # ---- helpers ----

    @staticmethod
    def to_patch(preset: dict[str, Any]) -> dict[str, Any]:
        """把预设展开成覆盖层 patch（{"openai": {...}}）。

        短名字段用 ``setdefault`` 语义：缺失的键不写 null，
        避免把用户没填的字段清成空（null 在覆盖层 = 删除回落）。
        """
        patch: dict[str, Any] = {}
        for short, dotted in FIELD_PATHS.items():
            if short not in preset:
                continue
            value = preset.get(short)
            if value is None:
                continue
            parts = dotted.split(".")
            node = patch
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return patch


# 模块级单例：Admin API 共享同一份预设状态
backend_presets = BackendPresets()
