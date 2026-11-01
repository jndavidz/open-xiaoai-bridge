//! PCM 流式代理 spike（临时验证代码，**不是正式实现**，见 docs/plan/pending-followups.md「PCM 流式代理 spike」）。
//!
//! 复用 doubao TTS 推流基建的思路（解码 → 分块 `send_stream("play")` → token 中断），
//! 把音源换成音乐文件/直链，播放参数升级到音乐规格（48k/S16 立体声等）。只验证三件事：
//! ① 音质——client PCM 通道放 48k/S16 立体声是否正常；
//! ② 切换体验——音乐推流中插 TTS 再续推（start_ms offset）的延迟与爆音；
//! ③ NAS CPU——解码+推流的持续占用。
//!
//! 设计约束：**自包含、不碰生产路径**——不复用/不改 `mod.rs` 的单声道
//! PcmPlaybackBuffer/throttle（那些按 `rate*2` 字节/秒算，是单声道假设），
//! 通道感知的数学在本文件内重写；生产行为零变化（除新增 pyfunction）。
//!
//! 简化（对 spike 判据无影响，正式实现需重做）：HTTP 源先整文件下载进内存再
//! 解码推流——拉流带宽不是本次风险点；解码是逐 packet 持续进行的，CPU 结论有效。
//! 续推（start_ms>0）从流头解码、解码后丢弃，跳段成本 = 解码耗时（见日志 skipped 段）。

use crate::tts::{cancel_playback_session, cancel_playback_session_if_active};
use crate::PLAYER_READY;
use open_xiaoai::services::connect::message::MessageManager;
use open_xiaoai::services::connect::rpc::RPC;
use pyo3::prelude::*;
use serde_json::json;
use std::io;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

use symphonia::core::audio::SampleBuffer;
use symphonia::core::codecs::{DecoderOptions, CODEC_TYPE_NULL};
use symphonia::core::errors::Error as SymphoniaError;
use symphonia::core::formats::FormatOptions;
use symphonia::core::io::{MediaSource, MediaSourceStream, ReadOnlySource};
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;

const SPIKE_WS_CHUNK: usize = 1024 * 1024; // 与生产 PLAY_CHUNK_SIZE 一致
const SPIKE_START_BUFFER_MS: u32 = 240; // 与生产 PCM_START_BUFFER_MS 一致
const SPIKE_STREAM_CHUNK_MS: u32 = 60; // 与生产 PCM_STREAM_CHUNK_MS 一致
const SPIKE_MAX_AHEAD_MS: u128 = 1500; // 与生产 MAX_AHEAD_MS 一致

fn is_active(token: u64) -> bool {
    crate::tts::is_playback_session_active(token)
}

/// spike：以指定参数启动设备侧 aplay（生产 `playback_config` 硬编码 24k/1ch，无法承载音乐规格）。
/// 成功后置 PLAYER_READY=true，让 TTS 管线的 ensure_player_started 不再覆盖；
/// 切回 TTS 前必须先 spike_stop（复位 PLAYER_READY=false，TTS 才会按 24k 重启 aplay）。
/// 一般不用直接调：`spike_stream_source` 会按文件真实规格自动启动 aplay。
#[pyfunction]
#[pyo3(signature = (sample_rate, channels, period_size=None, buffer_size=None))]
pub fn spike_start_play(
    py: Python<'_>,
    sample_rate: u32,
    channels: u16,
    period_size: Option<u32>,
    buffer_size: Option<u32>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        spike_remote_start_play(sample_rate, channels as usize, period_size, buffer_size).await;
        Ok(())
    })
}

/// 按 50ms period / 200ms buffer（与生产 24k 下 1200/4800 的时间跨度对齐）下发 start_play。
async fn spike_remote_start_play(
    sample_rate: u32,
    channels: usize,
    period: Option<u32>,
    buffer: Option<u32>,
) {
    let period = period.unwrap_or(sample_rate / 20);
    let buffer = buffer.unwrap_or(sample_rate / 5);
    let res = RPC::instance()
        .call_remote(
            "start_play",
            Some(json!({
                "pcm": "noop",
                "channels": channels,
                "bits_per_sample": 16,
                "sample_rate": sample_rate,
                "period_size": period,
                "buffer_size": buffer,
            })),
            None,
        )
        .await;
    crate::pylog!(
        "[Spike] start_play rate={} ch={} period={} buffer={} result={}",
        sample_rate,
        channels,
        period,
        buffer,
        if res.is_ok() { "ok" } else { "err" },
    );
    PLAYER_READY.store(true, Ordering::SeqCst);
}

/// spike：token 失效 + 杀 aplay + 复位 PLAYER_READY（一次调用完成全部拆除，
/// 之后 TTS 会按自己的 24k/1ch 重启 aplay）。
#[pyfunction]
#[pyo3(signature = (token=None))]
pub fn spike_stop(token: Option<u64>) {
    match token {
        Some(t) => {
            cancel_playback_session_if_active(t);
        }
        None => cancel_playback_session(),
    }
    PLAYER_READY.store(false, Ordering::SeqCst);
    let rt = pyo3_async_runtimes::tokio::get_runtime();
    rt.spawn(async {
        let _ = RPC::instance().call_remote("stop_play", None, None).await;
    });
}

/// spike：后台推流音乐文件（本地路径或 http/https 直链）。立即返回 token；
/// aplay 由 pipeline 按文件真实规格（rate/channels）启动；进度/结论看 `[Spike]` 日志。
#[pyfunction]
#[pyo3(signature = (source, start_ms=0, playback_token=None))]
pub fn spike_stream_source(
    py: Python<'_>,
    source: String,
    start_ms: u64,
    playback_token: Option<u64>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let token = playback_token.unwrap_or_else(crate::tts::begin_playback_session);
        tokio::spawn(spike_pipeline(source, start_ms, token));
        Ok::<u64, PyErr>(token)
    })
}

fn extension_of(source: &str) -> String {
    let path = source.split(['?', '#']).next().unwrap_or(source);
    let name = path.rsplit('/').next().unwrap_or(path);
    match name.rsplit_once('.') {
        Some((stem, ext)) if !stem.is_empty() && ext.len() <= 5 => ext.to_ascii_lowercase(),
        _ => "wav".to_string(),
    }
}

async fn fetch_source_bytes(source: &str) -> Result<Vec<u8>, String> {
    if source.starts_with("http://") || source.starts_with("https://") {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(300))
            .build()
            .map_err(|e| format!("http client build failed: {}", e))?;
        let resp = client
            .get(source)
            .send()
            .await
            .map_err(|e| format!("fetch failed: {}", e))?;
        if !resp.status().is_success() {
            return Err(format!("fetch http status {}", resp.status()));
        }
        let bytes = resp
            .bytes()
            .await
            .map_err(|e| format!("fetch body failed: {}", e))?;
        Ok(bytes.to_vec())
    } else {
        std::fs::read(source).map_err(|e| format!("read file failed (path={}): {}", source, e))
    }
}

/// 推流节流：设备收到的音频不得超前实时播放 SPIKE_MAX_AHEAD_MS。
/// 通道感知（bytes_per_ms = rate*channels*2/1000），与生产单声道版逻辑同形。
async fn spike_throttle(pcm_sent: usize, bytes_per_ms: usize, started: Instant, token: u64) {
    if bytes_per_ms == 0 {
        return;
    }
    let sent_ms = (pcm_sent / bytes_per_ms) as u128;
    let ahead = sent_ms.saturating_sub(started.elapsed().as_millis());
    if ahead > SPIKE_MAX_AHEAD_MS {
        let mut left = (ahead - SPIKE_MAX_AHEAD_MS) as u64;
        while left > 0 && is_active(token) {
            let step = left.min(50);
            tokio::time::sleep(Duration::from_millis(step)).await;
            left = left.saturating_sub(step);
        }
    }
}

async fn spike_send(pcm: Vec<u8>, token: u64) -> bool {
    if !is_active(token) {
        return false;
    }
    let mut offset = 0;
    while offset < pcm.len() {
        if !is_active(token) {
            return false;
        }
        let end = (offset + SPIKE_WS_CHUNK).min(pcm.len());
        let _ = MessageManager::instance()
            .send_stream("play", pcm[offset..end].to_vec(), None)
            .await;
        offset = end;
    }
    is_active(token)
}

/// 推流分块器：启动水位 240ms、稳态 60ms 块，全部按 frame 对齐（bytes_per_ms 整数倍）。
struct PushChunker {
    bytes_per_ms: usize,
    startup_bytes: usize,
    chunk_bytes: usize,
    queue: Vec<u8>,
    started: bool,
}

impl PushChunker {
    fn new(sample_rate: u32, channels: usize) -> Self {
        let bytes_per_ms = (sample_rate as usize * channels * 2) / 1000;
        let chunk_bytes = (bytes_per_ms * SPIKE_STREAM_CHUNK_MS as usize).max(2);
        Self {
            bytes_per_ms: bytes_per_ms.max(1),
            startup_bytes: (bytes_per_ms * SPIKE_START_BUFFER_MS as usize).max(chunk_bytes),
            chunk_bytes,
            queue: Vec::new(),
            started: false,
        }
    }

    fn ready(&mut self) -> Vec<Vec<u8>> {
        if !self.started {
            if self.queue.len() < self.startup_bytes {
                return Vec::new();
            }
            self.started = true;
        }
        self.drain_chunks()
    }

    /// 流结束后取全部剩余；尾巴按 20ms 粒度对齐裁掉零头（丢 <20ms 尾音，spike 可接受）。
    fn remaining(&mut self) -> Vec<Vec<u8>> {
        self.started = true;
        let mut chunks = self.drain_chunks();
        let align = (self.bytes_per_ms * 20).max(2);
        let tail = self.queue.len() - self.queue.len() % align;
        if tail > 0 {
            chunks.push(self.queue.drain(..tail).collect());
        }
        self.queue.clear();
        chunks
    }

    fn drain_chunks(&mut self) -> Vec<Vec<u8>> {
        let mut chunks = Vec::new();
        while self.queue.len() >= self.chunk_bytes {
            chunks.push(self.queue.drain(..self.chunk_bytes).collect());
        }
        chunks
    }
}

enum DecMsg {
    /// 首个成功解码包的规格（输出恒为 S16LE 交错）
    Spec { rate: u32, channels: usize },
    Pcm(Vec<u8>),
}

async fn spike_pipeline(source: String, start_ms: u64, token: u64) {
    let result = run_spike_pipeline(&source, start_ms, token).await;
    match result {
        Ok(summary) => {
            crate::pylog!("[Spike] summary: {}", summary);
        }
        Err(e) => {
            crate::pylog_error!("[Spike] pipeline failed (source={}): {}", source, e);
        }
    }
}

async fn run_spike_pipeline(source: &str, start_ms: u64, token: u64) -> Result<String, String> {
    let wall = Instant::now();
    let fetch_started = Instant::now();
    let data = fetch_source_bytes(source).await?;
    let fetch_ms = fetch_started.elapsed().as_millis();
    crate::pylog!(
        "[Spike] fetched {} bytes in {} ms (source={})",
        data.len(),
        fetch_ms,
        source
    );

    let ext = extension_of(source);
    let cursor = io::Cursor::new(data);
    let boxed: Box<dyn MediaSource> = Box::new(ReadOnlySource::new(cursor));
    let mss = MediaSourceStream::new(boxed, Default::default());
    let mut hint = Hint::new();
    hint.with_extension(&ext);

    let probed = symphonia::default::get_probe()
        .format(
            &hint,
            mss,
            &FormatOptions::default(),
            &MetadataOptions::default(),
        )
        .map_err(|e| format!("probe failed (ext={}): {}", ext, e))?;
    let mut reader = probed.format;
    let track = reader
        .tracks()
        .iter()
        .find(|t| t.codec_params.codec != CODEC_TYPE_NULL)
        .ok_or("no audio track")?
        .clone();
    let mut decoder = symphonia::default::get_codecs()
        .make(&track.codec_params, &DecoderOptions::default())
        .map_err(|e| format!("decoder make failed: {}", e))?;

    // 解码任务与推流任务解耦：解码尽快跑（受 channel 背压限制），推流按实时节拍。
    let (tx, mut rx) = mpsc::channel::<DecMsg>(32);
    let decode_handle = tokio::spawn(async move {
        let mut decode_total_us: u64 = 0;
        let mut decode_packets: u64 = 0;
        let mut skip_frames_left: u64 = 0;
        let mut spec_sent = false;
        loop {
            let packet = match reader.next_packet() {
                Ok(p) => p,
                Err(SymphoniaError::IoError(ref e))
                    if e.kind() == io::ErrorKind::UnexpectedEof =>
                {
                    break; // 正常流结束
                }
                Err(SymphoniaError::DecodeError(_)) => continue, // 跳过坏帧
                Err(e) => {
                    crate::pylog!("[Spike] reader end: {}", e);
                    break;
                }
            };
            if packet.track_id() != track.id {
                continue;
            }
            let d_start = Instant::now();
            let decoded = match decoder.decode(&packet) {
                Ok(d) => d,
                Err(_) => continue,
            };
            decode_total_us += d_start.elapsed().as_micros() as u64;
            decode_packets += 1;

            let spec_now = *decoded.spec();
            let rate = spec_now.rate;
            let channels = spec_now.channels.count();
            let mut sample_buf = SampleBuffer::<i16>::new(decoded.capacity() as u64, spec_now);
            sample_buf.copy_interleaved_ref(decoded);
            let mut samples: Vec<i16> = sample_buf.samples().to_vec();

            if !spec_sent {
                if tx.send(DecMsg::Spec { rate, channels }).await.is_err() {
                    break;
                }
                spec_sent = true;
                skip_frames_left = start_ms * u64::from(rate) / 1000;
            }

            // start_ms 跳段（frame 对齐，跨包累积）
            if skip_frames_left > 0 {
                let total_frames = (samples.len() / channels) as u64;
                let drop = skip_frames_left.min(total_frames);
                samples.drain(..(drop as usize) * channels);
                skip_frames_left -= drop;
                if skip_frames_left > 0 {
                    continue; // 整包都在跳段窗口内
                }
            }

            let mut bytes = Vec::with_capacity(samples.len() * 2);
            for s in &samples {
                bytes.extend_from_slice(&s.to_le_bytes());
            }
            if tx.send(DecMsg::Pcm(bytes)).await.is_err() {
                break; // 推流端已停
            }
        }
        (decode_total_us, decode_packets)
    });

    // 推流端：等规格 → 按规格启动 aplay → 240ms 水位起推 → 60ms 块节流推送
    let (rate, channels) = match rx.recv().await {
        Some(DecMsg::Spec { rate, channels }) => (rate, channels),
        Some(DecMsg::Pcm(_)) => return Err("spec missing before pcm".to_string()),
        None => return Err("decode ended before producing audio".to_string()),
    };
    let spec_ms = wall.elapsed().as_millis();
    spike_remote_start_play(rate, channels, None, None).await;
    let aplay_ms = wall.elapsed().as_millis();

    let mut chunker = PushChunker::new(rate, channels);
    let mut pcm_sent: usize = 0;
    let mut push_start: Option<Instant> = None;
    let mut first_push_ms: u128 = 0;
    let mut stream_open = true;
    let mut pending = rx.recv().await;

    loop {
        let (pcm, final_drain) = match pending.take() {
            Some(DecMsg::Pcm(bytes)) => (bytes, false),
            Some(DecMsg::Spec { .. }) => {
                pending = rx.recv().await;
                continue;
            }
            None => {
                stream_open = false;
                (Vec::new(), true)
            }
        };
        if !pcm.is_empty() {
            chunker.queue.extend_from_slice(&pcm);
        }
        let chunks = if final_drain && !stream_open {
            chunker.remaining()
        } else {
            chunker.ready()
        };
        for chunk in chunks {
            if push_start.is_none() {
                push_start = Some(Instant::now());
                first_push_ms = wall.elapsed().as_millis();
            }
            let len = chunk.len();
            if !spike_send(chunk, token).await {
                decode_handle.abort();
                return Ok(format!(
                    "aborted: source={}, spec={}/{}ch, pushed_ms={}",
                    source,
                    rate,
                    channels,
                    pushed_ms(pcm_sent, rate, channels),
                ));
            }
            pcm_sent += len;
            spike_throttle(pcm_sent, chunker.bytes_per_ms, push_start.unwrap(), token).await;
            if !is_active(token) {
                decode_handle.abort();
                return Ok(format!(
                    "aborted(cancelled): pushed_ms={}",
                    pushed_ms(pcm_sent, rate, channels)
                ));
            }
        }
        if final_drain && !stream_open {
            break;
        }
        pending = rx.recv().await;
    }

    let (decode_total_us, decode_packets) = match decode_handle.await {
        Ok(v) => v,
        Err(e) => {
            return Ok(format!(
                "decode task ended abnormally: {} (pushed_ms={})",
                e,
                pushed_ms(pcm_sent, rate, channels)
            ))
        }
    };

    // 等设备把已推的尾段放完（token 失效则立刻退出）
    let pushed_total_ms = pushed_ms(pcm_sent, rate, channels);
    if let Some(start) = push_start {
        let remaining = u64::try_from(pushed_total_ms as u128).unwrap_or(0)
            .saturating_sub(u64::try_from(start.elapsed().as_millis()).unwrap_or(0));
        let mut left = remaining;
        while left > 0 && is_active(token) {
            let step = left.min(50);
            tokio::time::sleep(Duration::from_millis(step)).await;
            left -= step;
        }
    }

    Ok(format!(
        "done: source={}, ext={}, spec={}/{}ch, fetched_ms={}, spec_ms={}, aplay_ms={}, first_push_ms={}, pushed_ms={}, pushed_bytes={}, decode_total_ms={}, decode_packets={}, wall_ms={}",
        source,
        ext,
        rate,
        channels,
        fetch_ms,
        spec_ms,
        aplay_ms,
        first_push_ms,
        pushed_total_ms,
        pcm_sent,
        decode_total_us / 1000,
        decode_packets,
        wall.elapsed().as_millis(),
    ))
}

fn pushed_ms(pcm_sent: usize, rate: u32, channels: usize) -> u64 {
    let bytes_per_ms = (rate as usize * channels * 2) / 1000;
    if bytes_per_ms == 0 {
        0
    } else {
        (pcm_sent / bytes_per_ms) as u64
    }
}
