import numpy as np
import sherpa_onnx

from core.utils.config import ConfigManager
from core.utils.file import get_model_file_path


class _SherpaOnnx:
    def start(self):
        config = ConfigManager.instance()
        keywords_score = config.get_app_config("kws.keywords_score", 2.0)
        keywords_threshold = config.get_app_config("kws.keywords_threshold", 0.2)

        self.keyword_spotter = sherpa_onnx.KeywordSpotter(
            provider="cpu",
            num_threads=1,
            max_active_paths=8,
            keywords_score=keywords_score,
            keywords_threshold=keywords_threshold,
            num_trailing_blanks=0,
            keywords_file=get_model_file_path("keywords.txt"),
            tokens=get_model_file_path("tokens.txt"),
            encoder=get_model_file_path("encoder.onnx"),
            decoder=get_model_file_path("decoder.onnx"),
            joiner=get_model_file_path("joiner.onnx"),
        )
        self.stream = self.keyword_spotter.create_stream()

    def reset(self):
        """Reset the stream to discard any partial recognition state."""
        self.stream = self.keyword_spotter.create_stream()

    def kws(self, frames):
        """识别关键词。命中返回结果的完整对象（keyword/tokens/timestamps），无命中返回 None。

        incident §5 建议 6：暴露完整 KeywordResult 供命中留证（tokens/timestamps）。
        注：sherpa-onnx 的 KeywordResult **不提供置信度**（只有 keyword/tokens/timestamps），
        故无法记录 `score`；事故复盘可用 tokens 与时间戳辅助定位声源。
        """
        samples = np.frombuffer(frames, dtype=np.int16)
        samples = samples.astype(np.float32) / 32768.0
        self.stream.accept_waveform(16000, samples)
        while self.keyword_spotter.is_ready(self.stream):
            self.keyword_spotter.decode_stream(self.stream)
            result = self.keyword_spotter.get_result(self.stream)
            if result:
                self.keyword_spotter.reset_stream(self.stream)
                # result 是 KeywordResult：.keyword / .tokens / .timestamps
                try:
                    kw = (result.keyword or "").strip()
                except AttributeError:
                    # 兼容旧版 API（get_result 直接返回 str）
                    return {"keyword": str(result).lower(), "tokens": [], "timestamps": []}
                if not kw:
                    continue
                return {
                    "keyword": kw.lower(),
                    "tokens": list(getattr(result, "tokens", []) or []),
                    "timestamps": list(getattr(result, "timestamps", []) or []),
                }


SherpaOnnx = _SherpaOnnx()
