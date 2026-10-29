"""可选 GENA 事件增强（B-09，默认关闭）。

定位：**只做补充信号**，永远不替代 duration／位置／身份／意图。主动 Stop 主路径
（B-06 真机已验证）不依赖本模块。

规则：
  - 默认关闭；关闭时 `accept()` 一律忽略。
  - 订阅按服务器返回的有效期续订；到期/SID 失效/设备重启才重订阅，回调恢复先取快照。
  - 传输层用 SID + SEQ 处理重复、乱序与缺口；语义幂等由**播放代次**兜底：
    同代次内同义 NOTIFY 只生效一次，新代次（真实重播）不会被永久去重吞掉。
  - 校验回调来源、SID、正文上限与解析边界；任何异常都不触发 Stop/推进/恢复。
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field

DEFAULT_MAX_BODY = 64 * 1024
DEFAULT_READY_MAX_S = 1800.0
SEQ_WRAP = 2 ** 32


@dataclass
class Subscription:
    sid: str
    callback: str
    expires_at: float
    last_seq: int = 0
    last_key: tuple | None = None
    generation: int = 0
    needs_snapshot: bool = True
    events: int = 0


@dataclass
class GenaResult:
    accepted: bool
    action: str = ""
    reason: str = ""
    state: str = ""
    uri: str = ""
    resubscribe: bool = False
    snapshot: bool = False


# UPnP LastChange 用属性写法（val="..."），这里只认这个边界内的两个字段
_TRANSPORT_RE = re.compile(r"<TransportState[^>]*\bval=\"([A-Za-z_]+)\"")
_URI_RE = re.compile(r"<CurrentTrackURI[^>]*\bval=\"([^\"<]{0,2048})\"")


class GenaGate:
    def __init__(
        self,
        *,
        enabled: bool = False,
        allowed_sources: tuple[str, ...] = (),
        max_body: int = DEFAULT_MAX_BODY,
        clock=time.monotonic,
    ) -> None:
        self.enabled = bool(enabled)
        self.allowed_sources = tuple(allowed_sources or ())
        self.max_body = max(256, int(max_body))
        self.clock = clock
        self._subs: dict[str, Subscription] = {}
        self.last_result: GenaResult | None = None

    # ---- 订阅 ----

    def subscribe(self, callback: str, timeout_s: float = 300.0) -> Subscription:
        timeout = min(max(30.0, float(timeout_s)), DEFAULT_READY_MAX_S)
        sub = Subscription(
            sid=f"uuid:{uuid.uuid4()}",
            callback=str(callback),
            expires_at=self.clock() + timeout,
        )
        self._subs[sub.sid] = sub
        return sub

    def renew(self, sid: str, timeout_s: float = 300.0) -> Subscription | None:
        sub = self._subs.get(sid)
        if sub is None:
            return None
        timeout = min(max(30.0, float(timeout_s)), DEFAULT_READY_MAX_S)
        sub.expires_at = self.clock() + timeout
        sub.needs_snapshot = True   # 续订窗口内的事件可能丢失，先取快照
        return sub

    def unsubscribe(self, sid: str) -> bool:
        return self._subs.pop(sid, None) is not None

    def expired(self, sid: str) -> bool:
        sub = self._subs.get(sid)
        return sub is None or sub.expires_at <= self.clock()

    # ---- 播放代次（语义幂等）----

    def note_playback_started(self) -> None:
        for sub in self._subs.values():
            sub.generation += 1
            sub.last_key = None

    # ---- 事件 ----

    def accept(self, *, source: str, sid: str, seq, body: str) -> GenaResult:
        result = self._accept(source=source, sid=sid, seq=seq, body=body)
        self.last_result = result
        return result

    def _accept(self, *, source: str, sid: str, seq, body: str) -> GenaResult:
        if not self.enabled:
            return GenaResult(False, reason="disabled")
        if self.allowed_sources and str(source) not in self.allowed_sources:
            return GenaResult(False, reason="source_rejected")
        text = body if isinstance(body, str) else ""
        if len(text.encode("utf-8", "replace")) > self.max_body:
            return GenaResult(False, reason="body_too_large")
        sub = self._subs.get(str(sid or ""))
        if sub is None:
            return GenaResult(False, reason="unknown_sid", resubscribe=True)
        if sub.expires_at <= self.clock():
            return GenaResult(False, reason="sid_expired", resubscribe=True)

        try:
            seq_i = int(seq)
        except (TypeError, ValueError):
            return GenaResult(False, reason="bad_seq")
        if seq_i < 0 or seq_i >= SEQ_WRAP:
            return GenaResult(False, reason="bad_seq")
        if seq_i < sub.last_seq:
            return GenaResult(False, reason="stale_seq")          # 乱序：丢弃
        if seq_i == sub.last_seq and sub.last_seq != 0:
            return GenaResult(False, reason="duplicate_seq")      # 传输重复
        if sub.last_seq and seq_i != sub.last_seq + 1:
            sub.last_seq = seq_i
            sub.needs_snapshot = True
            return GenaResult(False, reason="seq_gap", snapshot=True)  # 缺口：先快照

        transport = _TRANSPORT_RE.search(text)
        uri_match = _URI_RE.search(text)
        if transport is None and uri_match is None:
            return GenaResult(False, reason="parse_error")

        state = (transport.group(1).upper() if transport else "")
        uri = (uri_match.group(1).strip() if uri_match else "")
        digest = hashlib.sha256(f"{state}|{uri}".encode("utf-8")).hexdigest()
        key = (digest, sub.generation)
        sub.last_seq = seq_i
        if key == sub.last_key:
            # 同代次内的同义 NOTIFY（不同 SEQ）：语义幂等，只生效一次
            return GenaResult(False, reason="duplicate_semantic", state=state, uri=uri)
        sub.last_key = key
        sub.events += 1
        return GenaResult(True, action="transport", state=state, uri=uri,
                          snapshot=sub.needs_snapshot)

    def snapshot_taken(self, sid: str) -> None:
        sub = self._subs.get(sid)
        if sub is not None:
            sub.needs_snapshot = False

    def snapshot(self) -> dict:
        now = self.clock()
        return {
            "enabled": self.enabled,
            "subscriptions": [
                {"sid": s.sid, "callback": s.callback,
                 "expires_in_s": round(max(0.0, s.expires_at - now), 1),
                 "last_seq": s.last_seq, "generation": s.generation,
                 "needs_snapshot": s.needs_snapshot, "events": s.events}
                for s in self._subs.values()
            ],
        }
