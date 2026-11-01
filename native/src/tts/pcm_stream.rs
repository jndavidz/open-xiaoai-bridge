//! 正式 PCM 音乐推流模块（pcm-backend spec 票 01，spike `spike.rs` 的重写与取代）。
//!
//! 与 spike 的差异（见 `.scratch/pcm-backend/issues/01-formal-pcm-streamer.md`）：
//!  - **流式拉取**：HTTP `Range` 分块拉取（`HttpRangeReader` 实现 symphonia
//!    `MediaSource`），不再整文件进内存；
//!  - **start_ms 走 symphonia seek**（帧对齐，返回实际落点 ms 做对齐校验），
//!    seek 失败回落「从流头解码丢弃」；spike 的整段跳过不复用；
//!  - **会话状态注册表**：`pcm_session_state` 把 pushed_ms/status 暴露给 Python，
//!    桥侧进度记账（票 03）与曲末判定（票 04）靠它，不依赖设备 status；
//!  - **通道感知**沿用 spike 的缓冲数学（48k/2ch），不复用 mod.rs 单声道
//!    `PcmPlaybackBuffer`；token/PLAYER_READY/stop_play 语义与 mod.rs 一致。
//!
//! 互斥语义（票 02 的底层前提）：音乐会话与 TTS 共用全局 PLAYBACK_TOKEN——
//! 新会话（TTS 或音乐）开始即令旧推流失效；aplay 参数切换（24k/1ch ↔ 48k/2ch）
//! 由 start_play RPC 重建，经 dmix 不抢通道。

use crate::tts::{begin_playback_session, cancel_playback_session, cancel_playback_session_if_active,
                 is_playback_session_active, MAX_AHEAD_MS, PCM_START_BUFFER_MS,
                 PCM_STREAM_CHUNK_MS, PLAY_CHUNK_SIZE};
use crate::PLAYER_READY;
use open_xiaoai::services::connect::message::MessageManager;
use open_xiaoai::services::connect::rpc::RPC;
use pyo3::prelude::*;
use serde_json::json;
use std::collections::HashMap;
use std::io::{self, Read, Seek, SeekFrom};
use std::sync::atomic::Ordering;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

use symphonia::core::audio::SampleBuffer;
use symphonia::core::codecs::{DecoderOptions, CODEC_TYPE_NULL};
use symphonia::core::errors::Error as SymphoniaError;
use symphonia::core::formats::{FormatOptions, SeekMode, SeekTo};
use symphonia::core::io::{MediaSource, MediaSourceStream, ReadOnlySource};
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;
use symphonia::core::units::Time;

/// seek 落点与请求偏移的最大容忍偏差（ms）：超出记 warning（VBR 无索引时的
/// 粗定位是预期行为，不报错——桥侧记账用 actual 值）。
const SEEK_TOLERANCE_MS: u64 = 500;
/// HTTP Range 拉取块大小：一次 RTT 取 256KB（48k/2ch ≈ 2.7s 音频），
/// 起播延迟与请求频率的折中。
const HTTP_BLOCK_BYTES: u64 = 256 * 1024;

fn is_active(token: u64) -> bool {
    is_playback_session_active(token)
}

/// 通道感知字节速率：S16LE 交错 PCM 每毫秒字节数（48k/2ch = 192）。
fn bytes_per_ms(rate: u32, channels: u16) -> usize {
    rate as usize * channels as usize * 2 / 1000
}

// ---- 会话状态注册表（桥侧进度记账的数据源） ----

#[derive(Clone, Debug)]
pub struct SessionRecord {
    /// starting | playing | completed | cancelled | failed
    pub status: &'static str,
    /// 本次推流已发送到设备的音频毫秒数（不含 start_ms 偏移）
    pub pushed_ms: u64,
    /// 请求的起播偏移
    pub start_ms: u64,
    /// symphonia seek 实际落点（对齐校验结果；未 seek = 0）
    pub actual_start_ms: u64,
    /// 流头解析出的时长（无索引格式为 None，桥侧回落 resolver 时长）
    pub duration_ms: Option<u64>,
    pub rate: u32,
    pub channels: u16,
    pub error: Option<String>,
}

static SESSIONS: OnceLock<Mutex<HashMap<u64, SessionRecord>>> = OnceLock::new();

fn sessions() -> &'static Mutex<HashMap<u64, SessionRecord>> {
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn sess_update(token: u64, f: impl FnOnce(&mut SessionRecord)) {
    if let Ok(mut map) = sessions().lock() {
        if let Some(rec) = map.get_mut(&token) {
            f(rec);
        }
    }
}

fn sess_insert(token: u64, start_ms: u64) {
    if let Ok(mut map) = sessions().lock() {
        map.insert(
            token,
            SessionRecord {
                status: "starting",
                pushed_ms: 0,
                start_ms,
                actual_start_ms: 0,
                duration_ms: None,
                rate: 0,
                channels: 0,
                error: None,
            },
        );
    }
}

// ---- HTTP Range 拉取（symphonia MediaSource 适配） ----

/// 基于 HTTP Range 的可 seek 读取器：顺序读走本地块缓存，seek 离开缓存窗口
/// 就重发 Range 请求。给 symphonia 做 probe/seek/流式解码，内存占用 = 单块。
struct HttpRangeReader {
    url: String,
    client: reqwest::blocking::Client,
    /// 已下载字节，覆盖 [buf_start, buf_start + buffer.len())
    buffer: Vec<u8>,
    buf_start: u64,
    pos: u64,
    /// 文件总长（首次响应的 Content-Range 或全量响应得知）
    len: Option<u64>,
}

impl HttpRangeReader {
    fn new(url: String) -> Self {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(60))
            .build()
            .unwrap_or_default();
        Self {
            url,
            client,
            buffer: Vec::new(),
            buf_start: 0,
            pos: 0,
            len: None,
        }
    }

    /// 确保从 `self.pos` 起至少 `want` 字节在本地缓存。
    fn fill(&mut self, want: usize) -> io::Result<()> {
        let buffered_end = self.buf_start + self.buffer.len() as u64;
        if self.pos >= self.buf_start && self.pos + want as u64 <= buffered_end {
            return Ok(());
        }
        if self.pos == buffered_end && self.len != Some(self.pos) {
            // 顺序续读：追加在缓存尾部
            self.fetch(buffered_end, HTTP_BLOCK_BYTES, true)?;
        } else {
            self.fetch(self.pos, HTTP_BLOCK_BYTES, false)?;
        }
        // 内存上限：只保留 pos 之后的窗口（ symphonia 对 mp3 的 seek 需要回退
        // 重新同步，回退越过 buf_start 时 fill 会重新发 Range 请求，正确性不变）
        let keep_from = (self.pos - self.buf_start) as usize;
        if keep_from > 0 {
            self.buffer.drain(..keep_from.min(self.buffer.len()));
            self.buf_start = self.pos;
        }
        Ok(())
    }

    fn fetch(&mut self, start: u64, block: u64, append: bool) -> io::Result<()> {
        let known_len = self.len;
        let req_end = known_len.map_or(start + block - 1, |l| (start + block).min(l) - 1);
        let resp = self
            .client
            .get(&self.url)
            .header("Range", format!("bytes={}-{}", start, req_end))
            .send()
            .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("range get: {}", e)))?;
        let status = resp.status();
        if !status.is_success() {
            return Err(io::Error::new(
                io::ErrorKind::Other,
                format!("range get http {}", status),
            ));
        }
        let mut content_total: Option<u64> = None;
        if status == reqwest::StatusCode::PARTIAL_CONTENT {
            // Content-Range: bytes s-e/total
            if let Some(cr) = resp.headers().get("Content-Range").and_then(|v| v.to_str().ok()) {
                content_total = cr.rsplit('/').next().and_then(|t| t.parse::<u64>().ok());
            }
        }
        let data = resp
            .bytes()
            .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("range body: {}", e)))?
            .to_vec();
        if status == reqwest::StatusCode::PARTIAL_CONTENT {
            self.len = content_total.or(self.len);
        } else if start != 0 {
            // 200 = 服务器忽略 Range：只有从头读才对得上
            return Err(io::Error::new(
                io::ErrorKind::Other,
                "server ignored Range request (got 200 for non-zero offset)",
            ));
        } else {
            self.len = Some(data.len() as u64);
        }
        let data_start = if status == reqwest::StatusCode::PARTIAL_CONTENT {
            start
        } else {
            0
        };
        if append && data_start == self.buf_start + self.buffer.len() as u64 {
            self.buffer.extend_from_slice(&data);
        } else {
            self.buffer = data;
            self.buf_start = data_start;
        }
        Ok(())
    }

    fn try_len(&mut self) -> io::Result<u64> {
        if let Some(l) = self.len {
            return Ok(l);
        }
        // 0-0 探针拿总长
        let resp = self
            .client
            .get(&self.url)
            .header("Range", "bytes=0-0")
            .send()
            .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("len probe: {}", e)))?;
        if resp.status() == reqwest::StatusCode::PARTIAL_CONTENT {
            if let Some(cr) = resp.headers().get("Content-Range").and_then(|v| v.to_str().ok()) {
                if let Some(total) = cr.rsplit('/').next().and_then(|t| t.parse::<u64>().ok()) {
                    self.len = Some(total);
                    return Ok(total);
                }
            }
        }
        Err(io::Error::new(
            io::ErrorKind::Other,
            "length probe: no Content-Range",
        ))
    }
}

impl Read for HttpRangeReader {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        if self.len == Some(self.pos) {
            return Ok(0);
        }
        self.fill(buf.len())?;
        let off = (self.pos - self.buf_start) as usize;
        let n = (self.buffer.len() - off).min(buf.len());
        if n == 0 {
            return Ok(0);
        }
        buf[..n].copy_from_slice(&self.buffer[off..off + n]);
        self.pos += n as u64;
        Ok(n)
    }
}

impl Seek for HttpRangeReader {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        let next = match pos {
            SeekFrom::Start(n) => n as i64,
            SeekFrom::Current(d) => self.pos as i64 + d,
            SeekFrom::End(d) => self.try_len()? as i64 + d,
        };
        if next < 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "seek before 0"));
        }
        self.pos = next as u64;
        Ok(self.pos)
    }
}

impl MediaSource for HttpRangeReader {
    fn is_seekable(&self) -> bool {
        true
    }
    fn byte_len(&self) -> Option<u64> {
        self.len
    }
}

/// 本地文件 / HTTP 直链统一入口。
fn open_source(source: &str) -> Result<Box<dyn MediaSource>, String> {
    if source.starts_with("http://") || source.starts_with("https://") {
        Ok(Box::new(HttpRangeReader::new(source.to_string())))
    } else {
        let file = std::fs::File::open(source)
            .map_err(|e| format!("read file failed (path={}): {}", source, e))?;
        Ok(Box::new(ReadOnlySource::new(file)))
    }
}

fn extension_of(source: &str) -> String {
    let path = source.split(['?', '#']).next().unwrap_or(source);
    let name = path.rsplit('/').next().unwrap_or(path);
    match name.rsplit_once('.') {
        Some((stem, ext)) if !stem.is_empty() && ext.len() <= 5 => ext.to_ascii_lowercase(),
        _ => "wav".to_string(),
    }
}

// ---- 解码（sync，跑在 spawn_blocking 里） ----

enum DecMsg {
    /// 流头信息（时长 + seek 落点），先于一切音频
    Info {
        duration_ms: Option<u64>,
        actual_start_ms: u64,
    },
    /// 首个成功解码包的规格（输出恒为 S16LE 交错）
    Spec { rate: u32, channels: u16 },
    Pcm(Vec<u8>),
}

struct DecodeStats {
    decode_total_us: u64,
    decode_packets: u64,
}

/// 发送流头信息；接收端已停（推流端退出）时返回 false，调用方直接收尾。
fn send_info(tx: &mpsc::Sender<DecMsg>, duration_ms: Option<u64>, actual_ms: u64) -> bool {
    tx.blocking_send(DecMsg::Info {
        duration_ms,
        actual_start_ms: actual_ms,
    })
    .is_ok()
}

/// 探测 → 可选 seek(start_ms) → 逐 packet 解码 → PCM 发给推流端。
/// seek 失败回落「从流头解码丢弃」（spike 语义），不致命。
fn decode_source(
    reader: Box<dyn MediaSource>,
    ext: &str,
    start_ms: u64,
    tx: mpsc::Sender<DecMsg>,
) -> Result<DecodeStats, String> {
    let mss = MediaSourceStream::new(reader, Default::default());
    let mut hint = Hint::new();
    hint.with_extension(ext);

    let probed = symphonia::default::get_probe()
        .format(
            &hint,
            mss,
            &FormatOptions::default(),
            &MetadataOptions::default(),
        )
        .map_err(|e| format!("probe failed (ext={}): {}", ext, e))?;
    let mut format = probed.format;
    let track = format
        .tracks()
        .iter()
        .find(|t| t.codec_params.codec != CODEC_TYPE_NULL)
        .ok_or("no audio track")?
        .clone();
    let time_base = track.codec_params.time_base;
    let duration_ms = track
        .codec_params
        .n_frames
        .zip(time_base)
        .map(|(frames, tb)| frames * 1000 * u64::from(tb.numer) / u64::from(tb.denom));

    let ts_to_ms = |ts: u64, tb: Option<symphonia::core::units::TimeBase>| -> u64 {
        match tb {
            Some(tb) => ts * 1000 * u64::from(tb.numer) / u64::from(tb.denom),
            None => 0,
        }
    };

    let mut skip_frames_left: u64 = 0;
    if start_ms > 0 {
        let request = Time::new(start_ms / 1000, (start_ms % 1000) as f64 / 1000.0);
        match format.seek(
            SeekMode::Accurate,
            SeekTo::Time {
                time: request,
                track_id: Some(track.id),
            },
        ) {
            Ok(seeked) => {
                let actual_ms = ts_to_ms(seeked.actual_ts, time_base);
                if actual_ms.abs_diff(start_ms) > SEEK_TOLERANCE_MS {
                    crate::pylog!(
                        "[Pcm] seek drift: requested {} ms, landed {} ms (ext={})",
                        start_ms,
                        actual_ms,
                        ext
                    );
                }
                if !send_info(&tx, duration_ms, actual_ms) {
                    return Ok(DecodeStats {
                        decode_total_us: 0,
                        decode_packets: 0,
                    });
                }
            }
            Err(e) => {
                // 回落：从流头解码丢弃（帧对齐，跨包累积），与 spike 同语义
                crate::pylog!("[Pcm] seek failed ({}), falling back to decode-skip", e);
                if let Some(rate) = track.codec_params.sample_rate {
                    skip_frames_left = start_ms * u64::from(rate) / 1000;
                }
                if !send_info(&tx, duration_ms, 0) {
                    return Ok(DecodeStats {
                        decode_total_us: 0,
                        decode_packets: 0,
                    });
                }
            }
        }
    } else if !send_info(&tx, duration_ms, 0) {
        return Ok(DecodeStats {
            decode_total_us: 0,
            decode_packets: 0,
        });
    }

    let mut decoder = symphonia::default::get_codecs()
        .make(&track.codec_params, &DecoderOptions::default())
        .map_err(|e| format!("decoder make failed: {}", e))?;

    let mut decode_total_us: u64 = 0;
    let mut decode_packets: u64 = 0;
    let mut spec_sent = false;
    loop {
        let packet = match format.next_packet() {
            Ok(p) => p,
            Err(SymphoniaError::IoError(ref e)) if e.kind() == io::ErrorKind::UnexpectedEof => {
                break; // 正常流结束
            }
            Err(SymphoniaError::DecodeError(_)) => continue, // 跳过坏帧
            Err(e) => {
                crate::pylog!("[Pcm] reader end: {}", e);
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
            if tx
                .blocking_send(DecMsg::Spec {
                    rate,
                    channels: channels as u16,
                })
                .is_err()
            {
                break; // 推流端已停
            }
            spec_sent = true;
        }

        // seek 失败时的回落跳段（frame 对齐，跨包累积）
        if skip_frames_left > 0 {
            let total_frames = (samples.len() / channels) as u64;
            let drop = skip_frames_left.min(total_frames);
            samples.drain(..(drop as usize) * channels);
            skip_frames_left -= drop;
            if skip_frames_left > 0 {
                continue;
            }
        }

        let mut bytes = Vec::with_capacity(samples.len() * 2);
        for s in &samples {
            bytes.extend_from_slice(&s.to_le_bytes());
        }
        if tx.blocking_send(DecMsg::Pcm(bytes)).is_err() {
            break;
        }
    }
    Ok(DecodeStats {
        decode_total_us,
        decode_packets,
    })
}
// ---- 推流（async，通道感知节流） ----

/// 推流分块器：启动水位 240ms、稳态 60ms 块，全部按 frame 对齐；
/// 尾巴按 20ms 粒度对齐裁掉零头。通道感知（bytes_per_ms = rate*channels*2/1000）。
struct MusicPushChunker {
    bytes_per_ms: usize,
    startup_bytes: usize,
    chunk_bytes: usize,
    queue: Vec<u8>,
    started: bool,
}

impl MusicPushChunker {
    fn new(sample_rate: u32, channels: u16) -> Self {
        let bytes_per_ms = bytes_per_ms(sample_rate, channels).max(1);
        let chunk_bytes = (bytes_per_ms * PCM_STREAM_CHUNK_MS as usize).max(2);
        Self {
            bytes_per_ms: bytes_per_ms.max(1),
            startup_bytes: (bytes_per_ms * PCM_START_BUFFER_MS as usize).max(chunk_bytes),
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

fn pushed_ms(pcm_sent: usize, rate: u32, channels: u16) -> u64 {
    let bpm = bytes_per_ms(rate, channels);
    if bpm == 0 {
        0
    } else {
        (pcm_sent / bpm) as u64
    }
}

/// 设备收到的音频不得超前实时播放 MAX_AHEAD_MS（通道感知版）。
async fn throttle(pcm_sent: usize, bytes_per_ms: usize, started: Instant, token: u64) {
    if bytes_per_ms == 0 {
        return;
    }
    let sent_ms = (pcm_sent / bytes_per_ms) as u128;
    let ahead = sent_ms.saturating_sub(started.elapsed().as_millis());
    if ahead > MAX_AHEAD_MS {
        let mut left = (ahead - MAX_AHEAD_MS) as u64;
        while left > 0 && is_active(token) {
            let step = left.min(50);
            tokio::time::sleep(Duration::from_millis(step)).await;
            left = left.saturating_sub(step);
        }
    }
}

async fn send_chunk(pcm: Vec<u8>, token: u64) -> bool {
    if !is_active(token) {
        return false;
    }
    let mut offset = 0;
    while offset < pcm.len() {
        if !is_active(token) {
            return false;
        }
        let end = (offset + PLAY_CHUNK_SIZE).min(pcm.len());
        let _ = MessageManager::instance()
            .send_stream("play", pcm[offset..end].to_vec(), None)
            .await;
        offset = end;
    }
    is_active(token)
}

/// 按 50ms period / 200ms buffer（与生产 24k 下 1200/4800 的时间跨度对齐）下发
/// start_play 以音乐规格重建 aplay，成功后置 PLAYER_READY（TTS 管线不再覆盖）。
async fn start_aplay(rate: u32, channels: u16) {
    let period = rate / 20;
    let buffer = rate / 5;
    let res = RPC::instance()
        .call_remote(
            "start_play",
            Some(json!({
                "pcm": "noop",
                "channels": channels,
                "bits_per_sample": 16,
                "sample_rate": rate,
                "period_size": period,
                "buffer_size": buffer,
            })),
            None,
        )
        .await;
    crate::pylog!(
        "[Pcm] start_play rate={} ch={} result={}",
        rate,
        channels,
        if res.is_ok() { "ok" } else { "err" },
    );
    PLAYER_READY.store(true, Ordering::SeqCst);
}

async fn run_pipeline(source: String, start_ms: u64, token: u64) {
    sess_insert(token, start_ms);
    let result = pipeline_inner(&source, start_ms, token).await;
    match result {
        Ok(summary) => {
            crate::pylog!("[Pcm] summary: {}", summary);
        }
        Err(e) => {
            crate::pylog_error!("[Pcm] pipeline failed (source={}): {}", source, e);
            sess_update(token, |rec| {
                rec.status = "failed";
                rec.error = Some(e.chars().take(200).collect());
            });
        }
    }
    // 清理：注册表只保留终态供桥侧查询，会话过多时回收旧终态
    if let Ok(mut map) = sessions().lock() {
        if map.len() > 16 {
            map.retain(|_, rec| !matches!(rec.status, "completed" | "cancelled" | "failed"));
        }
    }
}

async fn pipeline_inner(source: &str, start_ms: u64, token: u64) -> Result<String, String> {
    let wall = Instant::now();
    let ext = extension_of(source);
    let reader = open_source(source)?;
    let (tx, mut rx) = mpsc::channel::<DecMsg>(32);

    let decode_ext = ext.clone();
    let decode_start_ms = start_ms;
    let decode_handle = tokio::task::spawn_blocking(move || {
        decode_source(reader, &decode_ext, decode_start_ms, tx)
    });

    let (rate, channels) = loop {
        match rx.recv().await {
            Some(DecMsg::Info {
                duration_ms,
                actual_start_ms,
            }) => {
                sess_update(token, |rec| {
                    rec.duration_ms = duration_ms;
                    rec.actual_start_ms = actual_start_ms;
                });
            }
            Some(DecMsg::Spec { rate, channels }) => break (rate, channels),
            Some(DecMsg::Pcm(_)) => return Err("spec missing before pcm".to_string()),
            None => return Err("decode ended before producing audio".to_string()),
        }
    };
    sess_update(token, |rec| {
        rec.rate = rate;
        rec.channels = channels;
        rec.status = "playing";
    });
    start_aplay(rate, channels).await;

    let mut chunker = MusicPushChunker::new(rate, channels);
    let mut pcm_sent: usize = 0;
    let mut push_start: Option<Instant> = None;
    let mut stream_open = true;
    let mut pending = rx.recv().await;
    let mut aborted = false;

    loop {
        let (pcm, final_drain) = match pending.take() {
            Some(DecMsg::Pcm(bytes)) => (bytes, false),
            Some(DecMsg::Info { .. }) | Some(DecMsg::Spec { .. }) => {
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
            }
            let len = chunk.len();
            if !send_chunk(chunk, token).await {
                aborted = true;
                break;
            }
            pcm_sent += len;
            sess_update(token, |rec| {
                rec.pushed_ms = pushed_ms(pcm_sent, rate, channels);
            });
            throttle(pcm_sent, chunker.bytes_per_ms, push_start.unwrap(), token).await;
            if !is_active(token) {
                aborted = true;
                break;
            }
        }
        if aborted {
            decode_handle.abort();
            let pushed = pushed_ms(pcm_sent, rate, channels);
            sess_update(token, |rec| {
                rec.status = "cancelled";
                rec.pushed_ms = pushed;
            });
            return Ok(format!(
                "aborted: source={}, spec={}/{}ch, pushed_ms={}",
                source, rate, channels, pushed
            ));
        }
        if final_drain && !stream_open {
            break;
        }
        pending = rx.recv().await;
    }

    let stats = match decode_handle.await {
        Ok(Ok(v)) => v,
        Ok(Err(e)) => return Err(e),
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

    sess_update(token, |rec| {
        rec.status = "completed";
        rec.pushed_ms = pushed_total_ms;
    });
    Ok(format!(
        "done: source={}, ext={}, spec={}/{}ch, actual_start_ms(wall)={}, pushed_ms={}, pushed_bytes={}, decode_total_ms={}, decode_packets={}, wall_ms={}",
        source,
        ext,
        rate,
        channels,
        wall.elapsed().as_millis(),
        pushed_total_ms,
        pcm_sent,
        stats.decode_total_us / 1000,
        stats.decode_packets,
        wall.elapsed().as_millis(),
    ))
}

// ---- pyfunction 入口 ----

/// 后台推流音乐源（http/https 直链或本地路径），立即返回 token。
/// 起播 = begin_playback_session（令旧 TTS/音乐会话失效）→ spawn pipeline。
#[pyfunction]
#[pyo3(signature = (source, start_ms=0, playback_token=None))]
pub fn pcm_stream_start(
    py: Python<'_>,
    source: String,
    start_ms: u64,
    playback_token: Option<u64>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let token = playback_token.unwrap_or_else(begin_playback_session);
        tokio::spawn(run_pipeline(source, start_ms, token));
        Ok::<u64, PyErr>(token)
    })
}

/// 停止音乐推流：token 失效 + 杀 aplay + 复位 PLAYER_READY。
/// 只在该 token 仍是活跃会话时动 aplay——若已被 TTS 会话顶掉（token 失效），
/// 说明 TTS 已接管并按自己的规格重启了 aplay，此时不得连带杀掉 TTS 的播放。
/// `None` = 全局停（显式拆除，无条件复位）。
#[pyfunction]
#[pyo3(signature = (token=None))]
pub fn pcm_stream_stop(token: Option<u64>) {
    let was_active = match token {
        Some(t) => cancel_playback_session_if_active(t),
        None => {
            cancel_playback_session();
            true
        }
    };
    if was_active {
        PLAYER_READY.store(false, Ordering::SeqCst);
        let rt = pyo3_async_runtimes::tokio::get_runtime();
        rt.spawn(async {
            let _ = RPC::instance().call_remote("stop_play", None, None).await;
        });
    } else {
        crate::pylog!("[Pcm] stop on stale token {} skipped (session superseded)", token.unwrap_or(0));
    }
}

/// 查询推流会话状态（JSON）：status/pushed_ms/start_ms/actual_start_ms/
/// duration_ms/rate/channels/error。未知 token 返回 None。
#[pyfunction]
pub fn pcm_session_state(token: u64) -> Option<String> {
    let map = sessions().lock().ok()?;
    let rec = map.get(&token)?;
    Some(
        json!({
            "status": rec.status,
            "pushed_ms": rec.pushed_ms,
            "start_ms": rec.start_ms,
            "actual_start_ms": rec.actual_start_ms,
            "duration_ms": rec.duration_ms,
            "rate": rec.rate,
            "channels": rec.channels,
            "error": rec.error,
        })
        .to_string(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::net::TcpListener;
    use std::thread;

    // ---- MusicPushChunker（通道感知缓冲数学） ----

    #[test]
    fn chunker_48k_stereo_watermark_and_chunks() {
        let mut c = MusicPushChunker::new(48000, 2);
        assert_eq!(c.bytes_per_ms, 192);
        // 启动水位 240ms = 46080 字节
        assert_eq!(c.startup_bytes, 46080);
        assert_eq!(c.chunk_bytes, 11520); // 60ms
        assert!(c.ready().is_empty());
        c.queue.extend(std::iter::repeat(0u8).take(46080));
        let chunks = c.ready();
        assert_eq!(chunks.len(), 4); // 水位到后清空整块
        assert!(chunks.iter().all(|ch| ch.len() == 11520));
    }

    #[test]
    fn chunker_44k1_mono_scales_channels() {
        let c = MusicPushChunker::new(44100, 1);
        assert_eq!(c.bytes_per_ms, 88);
        assert_eq!(c.chunk_bytes, 88 * 60);
        assert_eq!(c.startup_bytes, 88 * 240);
    }

    #[test]
    fn chunker_remaining_aligns_tail_to_20ms() {
        let mut c = MusicPushChunker::new(48000, 2);
        // 不到水位，直接收尾：尾巴按 20ms（3840 字节）对齐
        c.queue.extend(std::iter::repeat(0u8).take(10000));
        let chunks = c.remaining();
        let total: usize = chunks.iter().map(|ch| ch.len()).sum();
        assert_eq!(total, 10000 - 10000 % 3840);
    }

    #[test]
    fn pushed_ms_channel_aware() {
        assert_eq!(pushed_ms(192_000, 48000, 2), 1000);
        assert_eq!(pushed_ms(88_000, 44100, 1), 1000);
        assert_eq!(pushed_ms(1000, 0, 0), 0);
    }

    // ---- HttpRangeReader（mock HTTP 服务器） ----

    /// 单请求 mock：支持 Range（206 + Content-Range），每连接一个请求。
    fn spawn_mock_server(data: &'static [u8]) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        thread::spawn(move || {
            for stream in listener.incoming() {
                let mut stream = match stream {
                    Ok(s) => s,
                    Err(_) => break,
                };
                let mut buf = [0u8; 4096];
                let n = std::io::Read::read(&mut stream, &mut buf).unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]);
                let range = req
                    .lines()
                    .find(|l| l.to_ascii_lowercase().starts_with("range:"))
                    .and_then(|l| l.split_once(':'))
                    .map(|(_, v)| v.trim().to_string());
                let header_and_body = match range.as_deref().and_then(parse_range) {
                    Some((s, e)) => {
                        let e = e.min(data.len() as u64 - 1);
                        let body = &data[s as usize..=e as usize];
                        (
                            format!(
                                "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {}-{}/{}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                                s, e, data.len(), body.len()
                            ),
                            body.to_vec(),
                        )
                    }
                    None => (
                        format!(
                            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            data.len()
                        ),
                        data.to_vec(),
                    ),
                };
                use std::io::Write;
                let (head, body) = header_and_body;
                let _ = stream.write_all(head.as_bytes());
                let _ = stream.write_all(&body);
            }
        });
        format!("http://{}/audio.mp3", addr)
    }

    fn parse_range(v: &str) -> Option<(u64, u64)> {
        let v = v.strip_prefix("bytes=")?;
        let (s, e) = v.split_once('-')?;
        Some((s.parse().ok()?, e.parse().ok()?))
    }

    #[test]
    fn range_reader_sequential_and_seek() {
        let url = spawn_mock_server(b"0123456789ABCDEF");
        let mut r = HttpRangeReader::new(url);
        let mut buf = [0u8; 4];
        assert_eq!(r.read(&mut buf).unwrap(), 4);
        assert_eq!(&buf, b"0123");
        assert_eq!(r.read(&mut buf).unwrap(), 4);
        assert_eq!(&buf, b"4567");
        // 回 seek
        r.seek(SeekFrom::Start(8)).unwrap();
        assert_eq!(r.read(&mut buf).unwrap(), 4);
        assert_eq!(&buf, b"89AB");
        // 负向相对 seek
        r.seek(SeekFrom::Current(-8)).unwrap();
        assert_eq!(r.read(&mut buf).unwrap(), 4);
        assert_eq!(&buf, b"4567");
        assert_eq!(r.byte_len(), Some(16));
    }

    #[test]
    fn range_reader_memory_is_windowed_not_whole_file() {
        // 票 01 目标：流式拉取不得整文件进内存。顺序读 1MB mock 文件，
        // 本地缓存必须保持单块窗口。
        static BIG: [u8; 1024 * 1024] = [7u8; 1024 * 1024];
        let url = spawn_mock_server(&BIG);
        let mut r = HttpRangeReader::new(url);
        let mut buf = [0u8; 8192];
        for i in 0..64 {
            assert_eq!(r.read(&mut buf).unwrap(), 8192);
            assert_eq!(buf[0], 7);
            let _ = i;
        }
        assert!(
            r.buffer.len() <= (HTTP_BLOCK_BYTES as usize) * 2,
            "buffer grew to {} bytes; windowing broken",
            r.buffer.len()
        );
    }

    #[test]
    fn mp3_decode_stream_pipeline() {
        // 用仓库内 mp3 资产覆盖 mp3 解码路径（symphonia mpa probe + 解码）。
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../core/assets/sounds/tts_notify.mp3");
        let data = std::fs::read(path).expect("asset mp3 missing");
        let (duration_ms, actual_start_ms, pcm) = collect_decode(data, "mp3", 0);
        assert!(duration_ms.is_none() || duration_ms.unwrap() > 0);
        assert_eq!(actual_start_ms, 0);
        assert!(!pcm.is_empty(), "mp3 decode produced no audio");
    }

    #[test]
    fn range_reader_eof_at_end() {
        let url = spawn_mock_server(b"0123456789");
        let mut r = HttpRangeReader::new(url);
        let mut all = Vec::new();
        r.read_to_end(&mut all).unwrap();
        assert_eq!(all, b"0123456789");
        let mut buf = [0u8; 4];
        assert_eq!(r.read(&mut buf).unwrap(), 0);
    }

    // ---- 解码管线（wav 内存端到端） ----

    /// 生成最小合法 wav（s16le 单声道 PCM）。
    fn make_wav(rate: u32, samples: &[i16]) -> Vec<u8> {
        let data_len = samples.len() * 2;
        let mut v = Vec::with_capacity(44 + data_len);
        v.extend_from_slice(b"RIFF");
        v.extend_from_slice(&((36 + data_len) as u32).to_le_bytes());
        v.extend_from_slice(b"WAVEfmt ");
        v.extend_from_slice(&16u32.to_le_bytes());
        v.extend_from_slice(&1u16.to_le_bytes()); // PCM
        v.extend_from_slice(&1u16.to_le_bytes()); // mono
        v.extend_from_slice(&rate.to_le_bytes());
        v.extend_from_slice(&(rate * 2).to_le_bytes()); // byte rate
        v.extend_from_slice(&2u16.to_le_bytes()); // block align
        v.extend_from_slice(&16u16.to_le_bytes()); // bits
        v.extend_from_slice(b"data");
        v.extend_from_slice(&(data_len as u32).to_le_bytes());
        for s in samples {
            v.extend_from_slice(&s.to_le_bytes());
        }
        v
    }

    fn collect_decode(
        data: Vec<u8>,
        ext: &'static str,
        start_ms: u64,
    ) -> (Option<u64>, u64, Vec<i16>) {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            let (tx, mut rx) = mpsc::channel::<DecMsg>(64);
            let cursor: Box<dyn MediaSource> =
                Box::new(ReadOnlySource::new(Cursor::new(data)));
            let handle = tokio::task::spawn_blocking(move || {
                decode_source(cursor, ext, start_ms, tx).unwrap()
            });
            let mut duration_ms = None;
            let mut actual_start_ms = 0u64;
            let mut rate = 0u32;
            let mut channels = 0u16;
            let mut pcm = Vec::new();
            while let Some(msg) = rx.recv().await {
                match msg {
                    DecMsg::Info {
                        duration_ms: d,
                        actual_start_ms: a,
                    } => {
                        duration_ms = d;
                        actual_start_ms = a;
                    }
                    DecMsg::Spec { rate: r, channels: c } => {
                        rate = r;
                        channels = c;
                    }
                    DecMsg::Pcm(bytes) => {
                        for b in bytes.chunks(2) {
                            pcm.push(i16::from_le_bytes([b[0], b[1]]));
                        }
                    }
                }
            }
            let stats = handle.await.unwrap();
            assert!(stats.decode_packets > 0);
            let _ = (rate, channels);
            (duration_ms, actual_start_ms, pcm)
        })
    }

    #[test]
    fn wav_decode_from_head() {
        let samples: Vec<i16> = (0..8000).map(|i| (i % 100) as i16).collect();
        let data = make_wav(8000, &samples);
        let (duration_ms, actual_start_ms, pcm) = collect_decode(data, "wav", 0);
        assert_eq!(duration_ms, Some(1000));
        assert_eq!(actual_start_ms, 0);
        assert_eq!(pcm.len(), 8000);
    }

    #[test]
    fn wav_decode_seek_lands_on_packet_boundary() {
        let samples: Vec<i16> = (0..8000).map(|i| (i % 251) as i16).collect();
        let data = make_wav(8000, &samples);
        let (duration_ms, actual_start_ms, pcm) = collect_decode(data, "wav", 500);
        assert_eq!(duration_ms, Some(1000));
        // WAV 是模拟分包：seek 只能落在包边界（≤ 请求时刻），actual_start_ms 如实回报，
        // 桥侧记账以它为准（spec 验收 ≤2s 误差远小于一个包）。
        assert!(actual_start_ms <= 500 && 500 - actual_start_ms <= 100);
        let frames = actual_start_ms * 8; // 8000Hz
        assert_eq!(pcm.len(), 8000 - frames as usize);
        // 内容对齐：首帧 = 原第 frames 样本
        assert_eq!(pcm[0], samples[frames as usize]);
    }

    // ---- 会话注册表 ----

    #[test]
    fn session_registry_roundtrip() {
        let token = 987_654_321;
        sess_insert(token, 1200);
        sess_update(token, |rec| {
            rec.status = "playing";
            rec.pushed_ms = 42;
            rec.rate = 48000;
            rec.channels = 2;
        });
        let mut map = sessions().lock().unwrap();
        let rec = map.get(&token).unwrap();
        assert_eq!(rec.status, "playing");
        assert_eq!(rec.pushed_ms, 42);
        assert_eq!(rec.start_ms, 1200);
        assert_eq!(rec.rate, 48000);
        map.remove(&token);
        assert!(map.get(&token).is_none());
    }
}
