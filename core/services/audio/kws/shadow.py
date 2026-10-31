"""影子 KWS：用候选模型对同一路音频并行检测，只记录、不唤醒。

用途：生产灰度前的影子测试——主 KWS 行为完全不变，影子命中写入
`data/shadow/kws-shadow-YYYYMMDD.jsonl`，用于对比「候选 vs 现网」在同一音频上的差异。

设计约束：
- 只在主 KWS 的检测线程内调用（单线程，无需额外锁）。
- 任何异常只记 WARNING 并自动停用，绝不影响主唤醒链路。
- 默认关闭；`kws.shadow.enabled=true` 且 `model_dir` 有效才启用。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from core.utils.logger import logger

RATE = 16000
CHUNK = 1440


def _pick(files, prefer_int8: bool, chunk: int):
    candidates = [p for p in files if f"chunk-{chunk}-" in p.name] or list(files)
    exact = [p for p in candidates if (".int8.onnx" in p.name) == prefer_int8]
    if exact:
        candidates = exact

    def epoch(path: Path) -> int:
        for part in path.stem.split("-"):
            if part.isdigit():
                return int(part)
        return -1

    return max(candidates, key=epoch) if candidates else None


def pick_model_files(model_dir: Path) -> dict:
    """现网 encoder.onnx 与候选 encoder-epoch-...chunk-16.onnx 均可发现。"""
    result = {}
    for kind, prefer_int8 in (("encoder", True), ("decoder", False), ("joiner", True)):
        paths = sorted(model_dir.glob(f"{kind}-*.onnx")) + sorted(model_dir.glob(f"{kind}.onnx"))
        path = _pick(paths, prefer_int8, 16)
        if path is None:
            raise FileNotFoundError(f"{model_dir} 缺少 {kind} 模型")
        result[kind] = path
    tokens = model_dir / "tokens.txt"
    if not tokens.is_file():
        raise FileNotFoundError(f"{model_dir} 缺少 tokens.txt")
    result["tokens"] = tokens
    return result


def build_keywords(words: list[str], model_dir: Path, target: Path) -> Path:
    """有 bpe.model 走 cjkchar+bpe，否则 ppinyin；显示名写原词。"""
    import sherpa_onnx

    tokens = model_dir / "tokens.txt"
    bpe = model_dir / "bpe.model"
    if bpe.is_file():
        encoded = sherpa_onnx.text2token(
            [word.upper() for word in words],
            tokens=str(tokens),
            tokens_type="cjkchar+bpe",
            bpe_model=str(bpe),
        )
    else:
        encoded = sherpa_onnx.text2token(
            [word.upper() for word in words], tokens=str(tokens), tokens_type="ppinyin"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf8") as handle:
        for word, tokens_row in zip(words, encoded):
            handle.write(" ".join(tokens_row) + f" @{word}\n")
    return target


class ShadowKws:
    """候选模型影子检测器（只记录命中，不触发唤醒）。"""

    def __init__(
        self,
        model_dir: str,
        words: list[str],
        score: float = 1.0,
        threshold: float = 0.25,
        log_dir: str | None = None,
    ) -> None:
        import sherpa_onnx

        self.model_dir = Path(model_dir)
        self.words = [word for word in words if word]
        self.score = score
        self.threshold = threshold
        self.enabled = False
        self._disabled_reason = ""
        self._hits = 0
        self._frames = 0
        base = Path(log_dir) if log_dir else Path("data/shadow")
        self._log_dir = base
        self._log_dir.mkdir(parents=True, exist_ok=True)

        try:
            files = pick_model_files(self.model_dir)
            keyword_file = build_keywords(
                self.words, self.model_dir, self._log_dir / "shadow-keywords.txt"
            )
            self._kws = sherpa_onnx.KeywordSpotter(
                provider="cpu",
                num_threads=1,
                max_active_paths=8,
                keywords_score=score,
                keywords_threshold=threshold,
                num_trailing_blanks=0,
                keywords_file=str(keyword_file),
                tokens=str(files["tokens"]),
                encoder=str(files["encoder"]),
                decoder=str(files["decoder"]),
                joiner=str(files["joiner"]),
            )
            self._stream = self._kws.create_stream()
            self.enabled = True
            logger.info(
                f"[KWS-Shadow] 已启用：model={self.model_dir.name} words={len(self.words)} "
                f"score={score} threshold={threshold}",
                module="KWS",
            )
        except Exception as exc:  # noqa: BLE001 - 影子失败不得影响主 KWS
            self._disabled_reason = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[KWS-Shadow] 初始化失败，影子停用：{self._disabled_reason}", module="KWS")

    def reset(self) -> None:
        if not self.enabled:
            return
        try:
            self._stream = self._kws.create_stream()
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def _disable(self, exc: Exception) -> None:
        self.enabled = False
        self._disabled_reason = f"{type(exc).__name__}: {exc}"
        logger.warning(f"[KWS-Shadow] 运行异常，影子停用：{self._disabled_reason}", module="KWS")

    def feed(self, frames: bytes, main_hit: bool = False) -> None:
        """喂一帧 16k/单声道/16bit PCM；命中写 JSONL，不触发任何动作。"""
        if not self.enabled or not frames:
            return
        try:
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            self._stream.accept_waveform(RATE, samples)
            self._frames += 1
            hits: list[str] = []
            while self._kws.is_ready(self._stream):
                self._kws.decode_stream(self._stream)
                result = self._kws.get_result(self._stream)
                if result:
                    keyword = getattr(result, "keyword", None) or str(result)
                    keyword = keyword.strip()
                    if keyword:
                        hits.append(keyword)
                    self._kws.reset_stream(self._stream)
            if hits:
                self._hits += len(hits)
                self._log(hits, main_hit)
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def _log(self, hits: list[str], main_hit: bool) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hits": hits,
            "main_hit": bool(main_hit),
            "frames": self._frames,
        }
        path = self._log_dir / f"kws-shadow-{time.strftime('%Y%m%d')}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 写盘失败仅告警
            logger.warning(f"[KWS-Shadow] 写日志失败：{exc}", module="KWS")

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "disabled_reason": self._disabled_reason,
            "model_dir": str(self.model_dir),
            "words": len(self.words),
            "frames": self._frames,
            "hits": self._hits,
        }
