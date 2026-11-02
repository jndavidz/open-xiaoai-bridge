"""播放期回声消除（far-end 参考，对齐后单抽头 NLMS）—— music-far spec 票 04。

对准「音箱自播放音乐掩蔽唤醒词」的物理成因：用播放期 far-end 参考把扬声器
自身的声音从麦克风信号里减掉，再交给唤醒检测。离线 NLMS 已证 ERLE 14.3 dB、
非劣（deploy/lx06/kws-beamform-ab/verify_ch6_reference.py 与 RUNBOOK §7）。

设备事实（docs/devices/lx06/mic-array-6ch-capture.md §10 / verify_ch6_reference）：
  - 8ch 采集里 ch6 与 ch7 逐样本相同，取其一作参考即可（默认 ch6）；
  - 参考相对麦克风**极性反转**（对齐后增益取负即可，不影响估计）；
  - 参考**领先**麦克风 3.9–4.1 ms ⇒ 对齐时把参考延迟 ref_lead_samples（默认 64
    @16k ≈ 4.0 ms，可按实测调）。

自适应规格：**对齐后单抽头 NLMS**（e = d - ĝ·x，ĝ 按块归一化步长更新）。
选型依据（实测教训，勿回退）：
  - 回声主导路径 = 参考经已知对齐后的直接声学耦合（近似单抽头增益），
    单抽头 NLMS 在合成与离线实测上都把该分量消到量化噪声底（ERLE ≫ 10 dB）；
  - 256 抽头**块级** LMS 在音乐（强有色）输入下无可用的批量规格：按块能量
    归一等效步长放大 ~taps 倍（win 行含历史样本），3 块即发散到 1e166；
    按 trace(XᵀX) 归一谱安全但每模态步长被 256 维摊薄，1s 只收敛 3%；
    批量冻结残差的「逐样本向量化」同理破坏稳定性（16 个秩一项叠加谱半径
    可达 16）。真·顺序逐样本 NLMS 稳定但在纯 Python 循环下 RTF ~0.22，
    预算放不下。单抽头批量更新是标量 NLMS（谱界 λ=1），step<2 恒稳定、
    每块收敛 step，是本物理场景下既快又稳的规格。

设计约束（spec Implementation Decisions「输入链」逐条对应）：
  - **独立模块**，默认关闭；启用与否由上游（票 05 唤醒输入通道）按播放态决定，
    本模块只实现 DSP 本体与旁路自守；
  - 旁路条件：参考全零 / 参考恒定（无信息）、滤波发散、双讲检测命中——
    旁路是**静默回落**：输出与输入逐样本一致（直通），不抛错、不留半截状态；
  - 纯 numpy，bytes 进（多声道交错 int16）bytes 出（1ch int16），接口与
    beamform.py 同风格（feed / reset / stats）。

延迟预算：算法**零前瞻**（输出只用历史样本），组延迟 = feed 块对齐开销，
stats()['latency_ms'] 恒 0.0——不引入帧积压。
"""

from __future__ import annotations

import numpy as np

RATE = 16000
# 块内批处理的子块长度（样本）：仅影响更新粒度与计数，不影响算法语义。
BLOCK = 128
# 双讲检测（Geigel 变体）：滤波器已介入（|ĝ|>0.05）后，块内 |d| 超过
# 0.7·|对齐参考| 的样本占比 > 阈值才判双讲。
# 实测教训（三处，勿回退）：
#   ① 经典 0.5 比例在回声增益 0.4 + 常规底噪下就会误杀——参考过零点附近
#      |x|→0，任何底噪都满足 |d| > 0.5|x|，实测 134 块误旁路 19 块，且旁路
#      直通把原始回声灌进输出，ERLE 被钉死在 ~8.5 dB（≈10·log10(134/19)）；
#   ② 占比阈值挡逐样本抖动，但 0.5 比例下过零点占比本身就有 5–10%，不够；
#   ③ 滤波器未介入（ĝ≈0）时不冻结——此时无适应状态可保护，且近端与参考
#      近似不相关，单抽头相关更新天然免疫；冻结反而造成「永不收敛」死锁。
DOUBLETALK_RATIO = 0.7
DOUBLETALK_FRACTION = 0.05
DOUBLETALK_MIN_GAIN = 0.05
# 发散判据（EMA 口径）：输出/输入功率的指数滑动平均（τ≈0.2s）比值超阈值。
# 收敛期瞬态被 EMA 平滑掉，不会误触发；真发散（回声路径突变后 e 持续 ≥2×
# 输入）EMA 稳定超过 3。
DIVERGENCE_EMA_ALPHA = 0.005
DIVERGENCE_EMA_RATIO = 3.0
# 守卫武装条件：曾收敛过（EMA ERLE > 3 dB）——「从能用变不能用」才叫发散。
DIVERGENCE_ARMED_ERLE_DB = 3.0
# 发散旁路的冷却时长（秒）：冷却期内直通并重置增益，冷却后重新收敛。
DIVERGENCE_COOLDOWN_S = 0.5


def _ref_is_unusable(ref: np.ndarray) -> str | None:
    """参考信号不可用判据：全零 / 恒定值（无信息，无法估计回声路径）。"""
    if ref.size == 0:
        return "ref_empty"
    peak = float(np.max(np.abs(ref)))
    if peak == 0.0:
        return "ref_zero"
    if float(np.max(ref)) == float(np.min(ref)):
        return "ref_constant"
    return None


class EchoCanceller:
    """单抽头 NLMS 回声消除：多声道交错 int16 PCM 进，1ch（消除后）int16 出。

    用法：
        aec = EchoCanceller(channels=8)          # 8ch 采集（6 麦 + 2 参考）
        mono = aec.feed(interleaved_pcm_bytes)   # 播放期逐块喂入
        aec.stats()                              # 旁路计数 / ERLE / 延迟

    旁路时输出 = 输入的 mic_channel 声道逐样本原样（int16 直通）。
    """

    def __init__(
        self,
        channels: int = 8,
        ref_channel: int = 6,
        mic_channel: int = 0,
        step_size: float = 0.8,
        ref_lead_samples: int = 64,
        ref_polarity_inverted: bool = True,
        sample_rate: int = RATE,
    ) -> None:
        if channels < 2:
            raise ValueError("EchoCanceller 需要多声道输入（含 far-end 参考）")
        if not (0 <= ref_channel < channels) or not (0 <= mic_channel < channels):
            raise ValueError("ref_channel / mic_channel 越界")
        if ref_channel == mic_channel:
            raise ValueError("ref_channel 与 mic_channel 不能相同")
        self.channels = int(channels)
        self.ref_channel = int(ref_channel)
        self.mic_channel = int(mic_channel)
        # 标量 NLMS 步长：谱界 λ=1 ⇒ step<2 恒稳定；0.8 每块收敛 80%，
        # 1000 块/s（BLOCK=128 → 125 块/s）下 <0.1s 完全收敛。
        self.step_size = float(step_size)
        # 参考超前对齐：把参考推入延迟线，使其与麦克风里的回声逐样本对齐。
        self.ref_lead_samples = max(0, int(ref_lead_samples))
        self.ref_polarity_inverted = bool(ref_polarity_inverted)
        self.sample_rate = int(sample_rate)
        self.reset()

    # ---- 状态 ----
    def reset(self) -> None:
        self._gain = 0.0  # 对齐后回声路径增益 ĝ（含极性符号）
        self._lead_hist = np.zeros(self.ref_lead_samples, dtype=np.float64)  # 对齐线
        self._cooldown_blocks = 0
        self._divergence_armed = False  # 曾收敛过才武装发散守卫
        self._in_power_ema = 0.0
        self._out_power_ema = 0.0
        self._erle_num = 0.0
        self._erle_den = 0.0
        self._counters = {
            "frames_in": 0,
            "blocks_processed": 0,
            "blocks_bypassed": 0,
            "bypass_ref_zero": 0,
            "bypass_ref_constant": 0,
            "bypass_double_talk": 0,
            "bypass_divergence": 0,
            "divergence_resets": 0,
        }

    # ---- 对外接口 ----
    def feed(self, pcm: bytes) -> bytes:
        """处理一帧多声道交错 int16 PCM；返回 1ch int16（消除后麦克风信号）。"""
        frame_bytes = self.channels * 2
        usable = len(pcm) - (len(pcm) % frame_bytes)
        if usable <= 0:
            return b""
        self._counters["frames_in"] += 1
        samples = (
            np.frombuffer(pcm[:usable], dtype=np.int16)
            .astype(np.float64)
            .reshape(-1, self.channels)
        )
        d_all = samples[:, self.mic_channel]
        ref_raw = samples[:, self.ref_channel]
        if self.ref_polarity_inverted:
            ref_raw = -ref_raw
        out = np.empty(len(d_all), dtype=np.int16)
        n = len(d_all)
        pos = 0
        while pos < n:
            end = min(pos + BLOCK, n)
            out[pos:end] = self._process_block(d_all[pos:end], ref_raw[pos:end])
            pos = end
        return out.tobytes()

    def stats(self) -> dict:
        """可观测面：旁路计数（按原因分列）、ERLE、增益、延迟预算。"""
        stats = dict(self._counters)
        erle = (
            10.0 * np.log10(self._erle_num / self._erle_den)
            if self._erle_den > 0 and self._erle_num > 0
            else None
        )
        stats["erle_db"] = None if erle is None else round(float(erle), 2)
        stats["echo_gain"] = round(float(self._gain), 6)
        stats["latency_ms"] = 0.0  # 零前瞻：无算法延迟，不引入帧积压
        stats["enabled"] = True
        return stats

    # ---- 内部：块处理（直通或 NLMS 消除） ----
    def _align_ref(self, ref_block: np.ndarray) -> np.ndarray:
        """把参考延迟 ref_lead_samples，使其与麦克风里的回声对齐。

        无论参考可用与否都必须推进延迟线（否则静音段后重新对不齐）。
        """
        if self.ref_lead_samples == 0:
            return ref_block
        joined = np.concatenate([self._lead_hist, ref_block])
        aligned = joined[: len(ref_block)].copy()
        self._lead_hist = joined[-self.ref_lead_samples :].copy()
        return aligned

    def _process_block(self, d_block: np.ndarray, ref_raw: np.ndarray) -> np.ndarray:
        """处理一个 ≤BLOCK 样本的块；旁路时原样返回（int16 直通）。"""
        self._counters["blocks_processed"] += 1
        d_float = d_block.astype(np.float64)

        # 参考推进永远执行（含旁路块），保证恢复后对齐线不陈旧。
        x = self._align_ref(ref_raw)

        unusable = _ref_is_unusable(ref_raw)
        if unusable == "ref_zero":
            self._counters["bypass_ref_zero"] += 1
            return self._bypass(d_block)
        if unusable == "ref_constant":
            self._counters["bypass_ref_constant"] += 1
            return self._bypass(d_block)

        if self._cooldown_blocks > 0:
            self._cooldown_blocks -= 1
            return self._bypass(d_block, reason="divergence")

        # 双讲（Geigel 变体）：近端语音幅值成片压过对齐参考 ⇒ 冻结并直通。
        if abs(self._gain) > DOUBLETALK_MIN_GAIN:
            exceed_fraction = float(
                np.mean(np.abs(d_float) > DOUBLETALK_RATIO * np.abs(x))
            )
            if exceed_fraction > DOUBLETALK_FRACTION:
                self._counters["bypass_double_talk"] += 1
                return self._bypass(d_block, reason="double_talk")

        # 消除：e = d - ĝ·x（x 已对齐）。
        e = d_float - self._gain * x

        # 发散守卫（EMA + 武装语义）：曾收敛过、且输出功率 EMA 持续显著高于
        # 输入功率 EMA ⇒ 判定发散，重置 + 冷却直通。收敛期瞬态被 EMA 平滑
        # 且未武装时不触发（按单块瞬时功率判定会把正常收敛误杀——实测教训）。
        in_power = float(np.mean(d_float**2)) + 1e-12
        out_power = float(np.mean(e**2)) + 1e-12
        self._in_power_ema += DIVERGENCE_EMA_ALPHA * (in_power - self._in_power_ema)
        self._out_power_ema += DIVERGENCE_EMA_ALPHA * (out_power - self._out_power_ema)
        if self._out_power_ema * DIVERGENCE_ARMED_ERLE_DB < self._in_power_ema:
            self._divergence_armed = True  # EMA ERLE > 3 dB ⇒ 武装
        if (
            self._divergence_armed
            and self._out_power_ema > DIVERGENCE_EMA_RATIO * self._in_power_ema
        ):
            self._divergence_armed = False
            self._in_power_ema = 0.0
            self._out_power_ema = 0.0
            self._counters["divergence_resets"] += 1
            self._gain = 0.0
            self._cooldown_blocks = int(
                round(DIVERGENCE_COOLDOWN_S * self.sample_rate / BLOCK)
            )
            return self._bypass(d_block, reason="divergence")

        # 标量 NLMS 增益更新：ĝ += step·Σ(e·x)/Σx²。单模态 ⇒ λ=1，
        # step<2 恒稳定；双讲/旁路块不更新（上面已 return）。
        energy = float(np.sum(x * x)) + 1e-12
        self._gain += self.step_size * float(np.dot(e, x)) / energy

        # ERLE 累计（参考活跃且已消除的块才计入）
        d_power = float(np.sum(d_float**2))
        if d_power > 0:
            self._erle_num += d_power
            self._erle_den += float(np.sum(e**2))

        # 输出与输入同量纲（raw int16 单位，未归一化）——直接取 e。
        return np.clip(e, -32768, 32767).astype(np.int16)

    def _bypass(self, d_block: np.ndarray, reason: str = "") -> np.ndarray:
        """静默回落：输出 = 输入逐样本一致；只计数，不抛错、不打断链路。"""
        self._counters["blocks_bypassed"] += 1
        if reason == "divergence":
            self._counters["bypass_divergence"] += 1
        return d_block.astype(np.int16)
