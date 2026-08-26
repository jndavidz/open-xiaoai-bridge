"""运行时配置覆盖层（Runtime Overrides）。

分层语义（高 → 低）：
    1. 面板/接口写入的 overrides（data/runtime-overrides.json，持久化）
    2. config.py 中的字面量与环境变量注入值
    3. 代码内默认值

后台面板通过本模块修改上游 API 地址 / model 规格 / API key 等，
写入 JSON 后触发 ConfigManager.reload_app_config() 热生效，无需重启进程，
也不对 config.py 源文件做任何文本手术（config.py 含函数定义且 key 来自
环境变量引用，文本替换不可靠）。

JSON 中的值语义：
    - 标量/dict：深合并覆盖底层配置
    - null：删除该覆盖键，回落到底层值（用于"清除覆盖"）
"""

import json
import os
import threading
from pathlib import Path
from typing import Any


def _default_overrides_path() -> Path:
    """默认覆盖文件路径：<bridge>/data/runtime-overrides.json。

    容器部署经 RUNTIME_OVERRIDES_PATH 指向持久化卷（如 /app/data/...）。
    """
    env_path = os.environ.get("RUNTIME_OVERRIDES_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    # core/utils/runtime_overrides.py -> parents[1] == bridge/
    return Path(__file__).resolve().parents[1] / "data" / "runtime-overrides.json"


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把 patch 深合并进 base（就地修改 base）并返回 base。

    - dict 与 dict 递归合并
    - patch 叶子为 None 表示删除 base 中对应键（回落底层值）
    - 其他类型直接覆盖
    """
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


class RuntimeOverrides:
    """持久化运行时配置覆盖（单例由模块级 ``runtime_overrides`` 提供）。"""

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else _default_overrides_path()
        self._lock = threading.RLock()
        self._overrides: dict[str, Any] = {}
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        """从磁盘加载覆盖（文件缺失/损坏时视为空并保留现场）。"""
        with self._lock:
            try:
                raw = self._path.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
                self._overrides = data if isinstance(data, dict) else {}
            except FileNotFoundError:
                self._overrides = {}
            except Exception as exc:  # 损坏不静默：打日志但不阻断启动
                from core.utils.logger import logger

                logger.error(
                    f"[RuntimeOverrides] Failed to load {self._path}: "
                    f"{type(exc).__name__}: {exc}"
                )
                self._overrides = {}
            return self._overrides

    def snapshot(self) -> dict[str, Any]:
        """返回覆盖层当前内容（深拷贝）。"""
        with self._lock:
            return json.loads(json.dumps(self._overrides, ensure_ascii=False))

    def contains(self, dotted_path: str) -> bool:
        """判断某个点分路径是否被覆盖（用于前端标注来源）。"""
        node: Any = self.snapshot()
        for part in dotted_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    def save(self, overrides: dict[str, Any]) -> None:
        """原子写盘（tmp + rename），目录不存在则创建。"""
        with self._lock:
            self._overrides = overrides
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(overrides, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self._path)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """深合并 patch 到覆盖层、落盘并返回最新覆盖内容。"""
        with self._lock:
            merged = deep_merge(self.snapshot(), patch)
            self.save(merged)
            return merged

    def apply_to(self, app_config: dict[str, Any]) -> dict[str, Any]:
        """把覆盖层应用到 APP_CONFIG（就地深合并），供 ConfigManager 调用。"""
        overrides = self.snapshot()
        if not overrides:
            return app_config
        return deep_merge(app_config, overrides)


# 模块级单例：ConfigManager 与 Admin API 共享同一份覆盖状态
runtime_overrides = RuntimeOverrides()
