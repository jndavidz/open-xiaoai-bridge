"""句读切分器（P1 ticket 03/04）——LLM 流式增量 → 句段序列。

规则（spec《P1 流式对话管线》Implementation Decisions「句读切分」）：

- 强标点（。！？；换行）：段内已积累 ≥4 字（含标点计）即切；
- 弱标点（，、：,;）：段内已积累 ≥12 字（含标点计）才切；
- 无标点累积 ≥20 字强制切。

口径说明：

- 「字数」= 段总字符数（含标点）。不足门槛的强标点不切——如「好。」
  继续积累，由后续标点凑足门槛或流结束 flush 收尾；
- ≥20 字强切按字符硬切（可能切断词，规则如此；门槛可构造参数化）；
- 纯内存纯函数式：无 I/O、无时钟、无随机——同一 feed 序列必得同一
  句段序列（ticket 04 真机听感验证时如需调门槛只改构造参数）。
"""

from __future__ import annotations

STRONG_PUNCT = "。！？；\n"
WEAK_PUNCT = "，、：,;"


class SentenceSegmenter:
    """流式句读切分：feed 增量文本，产出完成的句段；flush 收尾残余。"""

    __slots__ = ("_buf", "min_strong", "min_weak", "force_cut")

    def __init__(
        self,
        *,
        min_strong: int = 4,
        min_weak: int = 12,
        force_cut: int = 20,
    ):
        self.min_strong = max(1, int(min_strong))
        self.min_weak = max(1, int(min_weak))
        self.force_cut = max(1, int(force_cut))
        self._buf = ""

    @property
    def pending(self) -> str:
        """尚未成段的缓冲（观测/测试用）。"""
        return self._buf

    def feed(self, delta: str) -> list[str]:
        """喂入一段增量文本，返回本次新完成的句段（可能 0~N 段）。"""
        if not delta:
            return []
        self._buf += delta
        segments: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            segments.append(self._buf[:cut])
            self._buf = self._buf[cut:]
        return segments

    def flush(self) -> str | None:
        """流结束：返回残余尾段（非空时）；缓冲清空。"""
        tail = self._buf
        self._buf = ""
        return tail or None

    # ---- 内部 ----

    def _find_cut(self) -> int | None:
        """返回缓冲的下一个切点（段末 exclusive 索引）；无切点返回 None。"""
        buf = self._buf
        for i, ch in enumerate(buf):
            if ch in STRONG_PUNCT:
                if i + 1 >= self.min_strong:
                    return i + 1
            elif ch in WEAK_PUNCT:
                if i + 1 >= self.min_weak:
                    return i + 1
        if len(buf) >= self.force_cut:
            return self.force_cut
        return None
