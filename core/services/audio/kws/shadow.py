"""影子 KWS：用候选模型对同一路音频并行检测，只记录、不唤醒。

用途：生产灰度前的影子测试——主 KWS 行为完全不变，影子命中写入
`data/shadow/kws-shadow-YYYYMMDD.jsonl`，用于对比「候选 vs 现网」在同一音频上的差异。

设计约束：
- 只在主 KWS 的检测线程内调用（单线程，无需额外锁）。
- 任何异常只记 WARNING 并自动停用，绝不影响主唤醒链路。
- 默认关闭；`kws.shadow.enabled=true` 且 `model_dir` 有效才启用。

命中留证（shadow 观测取证，仿主路 `_audio_ring`）：
- 每个 arm 维护 ~3s 环形缓冲，`feed` 恒喂（含 paused/静音帧）；
- 命中或主命中（影子漏检）时把缓冲写 `data/kws_snapshots/shadow_<arm>_<ts>.pcm`，
  路径记进 JSONL `snapshot` 字段——没有它，「影子命中主路漏掉」与「影子误触发」无法区分；
- `vad_prob` 记录命中窗口内最大值（事件后复位），每 arm 快照数上限防磁盘增长。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from core.utils.logger import logger

RATE = 16000
CHUNK = 1440
SNAPSHOT_SECONDS = 3
SNAPSHOT_KEEP = 50


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
        arm: str = "strongest",
        max_active_paths: int = 16,
        snapshot_dir: str | None = None,
    ) -> None:
        import sherpa_onnx

        self.model_dir = Path(model_dir)
        self.words = [word for word in words if word]
        self.score = score
        self.threshold = threshold
        self.arm = arm
        self.max_active_paths = max(1, int(max_active_paths))
        self.enabled = False
        self._disabled_reason = ""
        self._hits = 0
        self._frames = 0
        self._cpu_ms = 0.0
        self._audio_ms = 0.0
        self._rtf = 0.0
        base = Path(log_dir) if log_dir else Path("data/shadow")
        self._log_dir = base
        self._log_dir.mkdir(parents=True, exist_ok=True)
        if snapshot_dir:
            self._snapshot_dir = Path(snapshot_dir)
        else:
            # 与主路 _SNAPSHOT_DIR 同目录（bridge 根下 data/kws_snapshots），前缀区分归属
            self._snapshot_dir = Path(__file__).resolve().parents[4] / "data" / "kws_snapshots"
        try:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001 - 落盘失败在 dump 时再兜底
            pass
        # 命中留证环形缓冲：~3s 的 16k/16bit/单声道 PCM（96000 字节）
        self._ring: list[bytes] = []
        self._ring_max_bytes = SNAPSHOT_SECONDS * RATE * 2
        self._vad_prob_max: float | None = None
        self._snapshots_written = 0

        try:
            files = pick_model_files(self.model_dir)
            keyword_file = build_keywords(
                self.words, self.model_dir, self._log_dir / "shadow-keywords.txt"
            )
            self._kws = sherpa_onnx.KeywordSpotter(
                provider="cpu",
                num_threads=1,
                max_active_paths=self.max_active_paths,
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
                f"[KWS-Shadow] 已启用：arm={self.arm} model={self.model_dir.name} "
                f"words={len(self.words)} score={score} threshold={threshold} "
                f"paths={self.max_active_paths}",
                module="KWS",
            )
        except Exception as exc:  # noqa: BLE001 - 影子失败不得影响主 KWS
            self._disabled_reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                f"[KWS-Shadow] 初始化失败，arm={self.arm} 影子停用："
                f"{self._disabled_reason}",
                module="KWS",
            )

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

    def feed(
        self,
        frames: bytes,
        main_hit: bool = False,
        context: dict | None = None,
    ) -> list[str]:
        """喂一帧 16k/单声道/16bit PCM；命中写 JSONL + PCM 快照，不触发任何动作。"""
        if not self.enabled or not frames:
            return []
        started = time.perf_counter()
        try:
            self._ring_append(frames)
            prob = (context or {}).get("vad_prob")
            if prob is not None:
                try:
                    p = float(prob)
                except (TypeError, ValueError):
                    p = None
                if p is not None and (self._vad_prob_max is None or p > self._vad_prob_max):
                    self._vad_prob_max = p
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
            cpu_ms = (time.perf_counter() - started) * 1000.0
            self._cpu_ms += cpu_ms
            self._audio_ms += len(frames) / 2.0 / RATE * 1000.0
            self._rtf = self._cpu_ms / self._audio_ms if self._audio_ms > 0 else 0.0
            # 主命中也要留痕：hits=[] + main_hit=true 表示「主命中但影子漏检」
            if hits or main_hit:
                self._log(hits, main_hit, context=context, cpu_ms=cpu_ms)
            return hits
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)
            return []

    def record_main_hit(self, keyword: str, context: dict | None = None) -> None:
        """记录主 KWS 命中（影子未在本帧产出候选命中）。"""
        if not self.enabled:
            return
        self._log([], True, context=context, main_keyword=keyword, cpu_ms=0.0)

    def _ring_append(self, frames: bytes) -> None:
        """追加一帧并按字节上限裁剪（保留最近 SNAPSHOT_SECONDS 秒）。"""
        self._ring.append(frames)
        excess = sum(len(chunk) for chunk in self._ring) - self._ring_max_bytes
        while excess > 0 and self._ring:
            excess -= len(self._ring[0])
            del self._ring[0]

    def _dump_snapshot(self) -> str | None:
        """把环形缓冲写成 `shadow_<arm>_<ts>.pcm`，返回路径；失败/空缓冲返回 None。"""
        if not getattr(self, "_ring", None):
            return None
        arm = getattr(self, "arm", "strongest")
        try:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
            path = self._snapshot_dir / (
                f"shadow_{arm}_{time.strftime('%Y%m%d-%H%M%S')}_"
                f"{int(time.time() * 1000) % 1000:03d}.pcm"
            )
            with open(path, "wb") as handle:
                handle.write(b"".join(self._ring))
            self._ring.clear()
            self._snapshots_written += 1
            self._cleanup_snapshots(arm)
            return str(path)
        except Exception as exc:  # noqa: BLE001 - 留证失败不得影响影子检测
            logger.debug(f"[KWS-Shadow] 快照落盘失败 arm={arm}: {exc}", module="KWS")
            return None

    def _cleanup_snapshots(self, arm: str) -> None:
        """只保留最近 SNAPSHOT_KEEP 个本 arm 的快照（按文件名时间戳排序）。"""
        try:
            files = sorted(self._snapshot_dir.glob(f"shadow_{arm}_*.pcm"))
            for old in files[:-SNAPSHOT_KEEP]:
                old.unlink()
        except Exception:  # noqa: BLE001
            pass

    def _log(
        self,
        hits: list[str],
        main_hit: bool,
        context: dict | None = None,
        main_keyword: str | None = None,
        cpu_ms: float = 0.0,
    ) -> None:
        snap_path = self._dump_snapshot()
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "arm": getattr(self, "arm", "strongest"),
            "hits": hits,
            "main_hit": bool(main_hit),
            "main_keyword": main_keyword,
            "frames": self._frames,
            "model": getattr(getattr(self, "model_dir", None), "name", ""),
            "words": len(getattr(self, "words", [])),
            "score": getattr(self, "score", None),
            "threshold": getattr(self, "threshold", None),
            "max_active_paths": getattr(self, "max_active_paths", None),
            "cpu_ms": round(float(cpu_ms), 2),
            "audio_ms": round(float(getattr(self, "_audio_ms", 0.0)), 1),
            "rtf": round(float(getattr(self, "_rtf", 0.0)), 4),
        }
        if context:
            record.update(context)
        # vad_prob 语义 = 命中窗口内最大值（feed 逐帧累计）；无累计值时回退当前帧值
        window_max = getattr(self, "_vad_prob_max", None)
        if window_max is not None:
            record["vad_prob"] = round(float(window_max), 4)
        self._vad_prob_max = None
        if snap_path:
            record["snapshot"] = snap_path
        path = self._log_dir / f"kws-shadow-{time.strftime('%Y%m%d')}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 写盘失败仅告警
            logger.warning(f"[KWS-Shadow] 写日志失败：{exc}", module="KWS")

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "arm": getattr(self, "arm", "strongest"),
            "disabled_reason": self._disabled_reason,
            "model_dir": str(self.model_dir),
            "words": len(self.words),
            "max_active_paths": self.max_active_paths,
            "frames": self._frames,
            "hits": self._hits,
            "cpu_ms": round(self._cpu_ms, 1),
            "audio_ms": round(self._audio_ms, 1),
            "rtf": round(self._rtf, 4),
            "snapshots": getattr(self, "_snapshots_written", 0),
            "snapshot_dir": str(getattr(self, "_snapshot_dir", "")),
        }
