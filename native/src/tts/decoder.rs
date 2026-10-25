use std::io;
use symphonia::core::audio::SampleBuffer;
use symphonia::core::codecs::{Decoder, DecoderOptions, CODEC_TYPE_NULL};
use symphonia::core::formats::{FormatOptions, Packet};
use symphonia::core::io::{MediaSource, MediaSourceStream, ReadOnlySource};
use symphonia::core::meta::MetadataOptions;
use symphonia::core::probe::Hint;

/// Map TTS API format string to file extension hint for symphonia probe.
fn format_to_extension(format: &str) -> &str {
    match format {
        "mp3" => "mp3",
        "ogg" | "ogg_opus" | "ogg_vorbis" => "ogg",
        "wav" => "wav",
        "flac" => "flac",
        "pcm" => "wav", // raw PCM treated as wav
        _ => format,    // fallback: use as-is
    }
}

/// Decode audio bytes into PCM (16-bit signed, mono).
/// Supports any format that symphonia can probe: MP3, OGG Vorbis, WAV, FLAC, etc.
/// Frame alignment is handled by symphonia's format reader.
pub fn decode_audio_to_pcm(
    audio_data: &[u8],
    format: &str,
    target_sample_rate: u32,
) -> Result<Vec<u8>, String> {
    if format == "pcm" {
        return Ok(audio_data.to_vec());
    }

    let cursor = io::Cursor::new(audio_data.to_vec());
    let source: Box<dyn MediaSource> = Box::new(ReadOnlySource::new(cursor));
    let mss = MediaSourceStream::new(source, Default::default());

    let mut hint = Hint::new();
    hint.with_extension(format_to_extension(format));

    let probed = symphonia::default::get_probe()
        .format(
            &hint,
            mss,
            &FormatOptions::default(),
            &MetadataOptions::default(),
        )
        .map_err(|e| format!("Probe failed (format={}): {}", format, e))?;

    let mut reader = probed.format;

    // Find the first audio track
    let track = reader
        .tracks()
        .iter()
        .find(|t| t.codec_params.codec != CODEC_TYPE_NULL)
        .ok_or("No audio track found")?;

    let track_id = track.id;
    let codec_params = track.codec_params.clone();

    let mut decoder = symphonia::default::get_codecs()
        .make(&codec_params, &DecoderOptions::default())
        .map_err(|e| format!("Decoder creation failed: {}", e))?;

    let source_sample_rate = codec_params.sample_rate.unwrap_or(24000);
    let channels = codec_params.channels.map(|c| c.count()).unwrap_or(1);

    let mut all_samples: Vec<i16> = Vec::new();
    loop {
        let packet = match reader.next_packet() {
            Ok(p) => p,
            Err(symphonia::core::errors::Error::IoError(ref e))
                if e.kind() == io::ErrorKind::UnexpectedEof =>
            {
                break;
            }
            Err(_) => break,
        };

        if packet.track_id() != track_id {
            continue;
        }

        decode_packet_into(&packet, decoder.as_mut(), channels, &mut all_samples);
    }

    Ok(finalize_samples(all_samples, source_sample_rate, target_sample_rate))
}

/// Decode one packet into interleaved i16 samples, mixed down to mono.
fn decode_packet_into(
    packet: &Packet,
    decoder: &mut dyn Decoder,
    channels: usize,
    out: &mut Vec<i16>,
) {
    let decoded = match decoder.decode(packet) {
        Ok(d) => d,
        Err(_) => return, // Skip corrupt frames
    };

    let spec = *decoded.spec();
    let num_frames = decoded.capacity();
    let mut sample_buf = SampleBuffer::<i16>::new(num_frames as u64, spec);
    sample_buf.copy_interleaved_ref(decoded);

    let samples = sample_buf.samples();

    if channels > 1 {
        // Mix down to mono
        for chunk in samples.chunks(channels) {
            let sum: i32 = chunk.iter().map(|&s| s as i32).sum();
            out.push((sum / channels as i32) as i16);
        }
    } else {
        out.extend_from_slice(samples);
    }
}

/// Resample (if needed) and convert mono i16 samples to little-endian PCM bytes.
fn finalize_samples(samples: Vec<i16>, from_rate: u32, to_rate: u32) -> Vec<u8> {
    let final_samples = if from_rate != to_rate {
        resample(&samples, from_rate, to_rate)
    } else {
        samples
    };

    let mut pcm_bytes = Vec::with_capacity(final_samples.len() * 2);
    for sample in &final_samples {
        pcm_bytes.extend_from_slice(&sample.to_le_bytes());
    }
    pcm_bytes
}

/// Simple linear interpolation resampler
fn resample(samples: &[i16], from_rate: u32, to_rate: u32) -> Vec<i16> {
    if from_rate == to_rate || samples.is_empty() {
        return samples.to_vec();
    }

    let ratio = from_rate as f64 / to_rate as f64;
    let output_len = (samples.len() as f64 / ratio).ceil() as usize;
    let mut output = Vec::with_capacity(output_len);

    for i in 0..output_len {
        let src_pos = i as f64 * ratio;
        let idx = src_pos as usize;
        let frac = src_pos - idx as f64;

        if idx + 1 < samples.len() {
            let s = samples[idx] as f64 * (1.0 - frac) + samples[idx + 1] as f64 * frac;
            output.push(s.round() as i16);
        } else if idx < samples.len() {
            output.push(samples[idx]);
        }
    }

    output
}

/// Stateful counterpart of [`resample`] producing bit-identical output when fed
/// incrementally: output index i is interpolated from global input samples
/// floor(i*ratio) and floor(i*ratio)+1, so segments only keep the input tail
/// that future outputs may still reference.
struct IncrementalResampler {
    passthrough: bool,
    ratio: f64,
    /// Global output index of the next sample to produce.
    next_out: usize,
    /// Total input samples consumed into the resampler so far (global count).
    total_in: usize,
    /// Input tail (global indices total_in - pending.len() .. total_in) that
    /// future outputs may still reference.
    pending: Vec<i16>,
    out: Vec<i16>,
}

impl IncrementalResampler {
    fn new(from_rate: u32, to_rate: u32) -> Self {
        Self {
            passthrough: from_rate == to_rate,
            ratio: from_rate as f64 / to_rate as f64,
            next_out: 0,
            total_in: 0,
            pending: Vec::new(),
            out: Vec::new(),
        }
    }

    /// Feed one batch of mono input samples and emit every output whose both
    /// interpolation sources have arrived.
    fn push(&mut self, segment: &[i16]) {
        if self.passthrough {
            self.total_in += segment.len();
            self.out.extend_from_slice(segment);
            return;
        }
        self.pending.extend_from_slice(segment);
        self.total_in += segment.len();
        let n = self.total_in;

        while let Some(out) = self.interpolate(self.next_out, n, false) {
            self.out.push(out);
            self.next_out += 1;
        }
        self.trim_pending();
    }

    /// After the stream ends, emit the remaining outputs up to
    /// ceil(total_in / ratio) using the same tail rule as [`resample`].
    fn flush(&mut self) {
        if self.passthrough {
            return;
        }
        let output_len = (self.total_in as f64 / self.ratio).ceil() as usize;
        let n = self.total_in;
        while self.next_out < output_len {
            match self.interpolate(self.next_out, n, true) {
                Some(out) => {
                    self.out.push(out);
                    self.next_out += 1;
                }
                None => break,
            }
        }
        self.trim_pending();
    }

    /// Compute output `i` given `available` input samples so far. Requires both
    /// interpolation sources, except when `is_final` (matching [`resample`]'s
    /// single-sample tail rule).
    fn interpolate(&self, i: usize, available: usize, is_final: bool) -> Option<i16> {
        let src_pos = i as f64 * self.ratio;
        let idx = src_pos as usize;
        let frac = src_pos - idx as f64;
        let base = self.total_in - self.pending.len();

        if idx < base {
            // Input tail was over-trimmed; cannot happen if trim_pending holds.
            return None;
        }
        if idx + 1 < available {
            let s0 = self.pending[idx - base] as f64;
            let s1 = self.pending[idx + 1 - base] as f64;
            let v = s0 * (1.0 - frac) + s1 * frac;
            Some(v.round() as i16)
        } else if is_final && idx < available {
            Some(self.pending[idx - base])
        } else {
            None
        }
    }

    /// Drop input samples no future output can reference (idx+1 < total_in).
    fn trim_pending(&mut self) {
        if self.next_out == 0 {
            return;
        }
        let keep_from = ((self.next_out as f64 * self.ratio) as usize).saturating_sub(1);
        let base = self.total_in - self.pending.len();
        if keep_from > base {
            self.pending.drain(..keep_from - base);
        }
    }
}

/// MPEG audio frame header tables (Layer III only; other layers fall back to
/// the whole-buffer decode path).
const BITRATES_V1_L3: [u32; 16] = [
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
];
const BITRATES_V2_L3: [u32; 16] = [
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0,
];
const SAMPLE_RATES_V1: [u32; 4] = [44100, 48000, 32000, 0];
const SAMPLE_RATES_V2: [u32; 4] = [22050, 24000, 16000, 0];
const SAMPLE_RATES_V25: [u32; 4] = [11025, 12000, 8000, 0];

struct Mp3FrameInfo {
    /// Total frame length in bytes including the 4-byte header.
    len: usize,
    /// Samples per frame (used as packet duration).
    samples: u64,
}

/// Parse an MPEG Layer III frame header (4 bytes at frame start).
/// Returns None for non-Layer-III, reserved fields, or free-format frames —
/// all of which disqualify incremental framing for this stream.
fn parse_mp3_frame_header(hdr: &[u8]) -> Option<Mp3FrameInfo> {
    if hdr.len() < 4 || hdr[0] != 0xFF || hdr[1] & 0xE0 != 0xE0 {
        return None;
    }
    let version_bits = (hdr[1] >> 3) & 0x03; // 0=MPEG2.5, 2=MPEG2, 3=MPEG1
    let layer_bits = (hdr[1] >> 1) & 0x03; // 1=Layer III
    if layer_bits != 1 {
        return None;
    }
    let (bitrate_table, rate_table, lsf): (&[u32; 16], &[u32; 4], bool) = match version_bits {
        3 => (&BITRATES_V1_L3, &SAMPLE_RATES_V1, false),
        2 => (&BITRATES_V2_L3, &SAMPLE_RATES_V2, true),
        0 => (&BITRATES_V2_L3, &SAMPLE_RATES_V25, true),
        _ => return None,
    };
    let bitrate_kbps = bitrate_table[(hdr[2] >> 4) as usize];
    let sample_rate = rate_table[((hdr[2] >> 2) & 0x03) as usize];
    if bitrate_kbps == 0 || sample_rate == 0 {
        return None; // free format or reserved
    }
    let padding = ((hdr[2] >> 1) & 0x01) as usize;
    // MPEG1 Layer III: 1152 samples/coefficient 144; MPEG2/2.5 LSF: 576/72.
    let (coef, samples) = if lsf { (72u32, 576u64) } else { (144u32, 1152u64) };
    let len = (coef * bitrate_kbps * 1000 / sample_rate) as usize + padding;
    if len <= 4 {
        return None;
    }
    Some(Mp3FrameInfo { len, samples })
}

/// ID3v2 tag total length (including the 10-byte header) if buf starts with one.
fn id3v2_total_len(buf: &[u8]) -> Option<usize> {
    if buf.len() < 10 || &buf[0..3] != b"ID3" {
        return None;
    }
    let size = ((buf[6] as usize) << 21)
        | ((buf[7] as usize) << 14)
        | ((buf[8] as usize) << 7)
        | (buf[9] as usize);
    Some(size + 10)
}

/// Locate the next complete MP3 frame at or after `offset`.
/// Returns Some((start, frame_info)) only when the whole frame is present in
/// `buf`; returns None when more data is needed. `scanned` accumulates how many
/// junk bytes were skipped so the caller can bail out on non-MP3 data.
fn next_mp3_frame(buf: &[u8], offset: usize, scanned: &mut usize) -> Option<(usize, Mp3FrameInfo)> {
    let mut pos = offset;
    while pos + 4 <= buf.len() {
        if buf[pos] == 0xFF && buf[pos + 1] & 0xE0 == 0xE0 {
            if let Some(info) = parse_mp3_frame_header(&buf[pos..pos + 4]) {
                if pos + info.len <= buf.len() {
                    return Some((pos, info));
                }
                // Valid header but incomplete frame: wait for more data.
                return None;
            }
        }
        pos += 1;
        *scanned += 1;
    }
    None
}

/// Give up incremental framing after this many junk bytes without a valid
/// Layer III frame header (non-MP3 data fed with format="mp3").
const MP3_SYNC_GIVE_UP: usize = 64 * 1024;

/// Probe a whole MP3 buffer once to create the persistent codec decoder.
/// Only reads the stream head, so a bounded prefix copy is enough.
fn probe_mp3_decoder(
    buf: &[u8],
) -> Result<(Box<dyn Decoder>, u32 /*source rate*/, usize /*channels*/), String> {
    // Probe never needs the tail; cap the copy for pathological buffers.
    let prefix: Vec<u8> = buf.iter().take(256 * 1024).copied().collect();
    let cursor = io::Cursor::new(prefix);
    let source: Box<dyn MediaSource> = Box::new(ReadOnlySource::new(cursor));
    let mss = MediaSourceStream::new(source, Default::default());

    let mut hint = Hint::new();
    hint.with_extension("mp3");

    let probed = symphonia::default::get_probe()
        .format(
            &hint,
            mss,
            &FormatOptions::default(),
            &MetadataOptions::default(),
        )
        .map_err(|e| format!("Probe failed (format=mp3): {}", e))?;

    let reader = probed.format;
    let track = reader
        .tracks()
        .iter()
        .find(|t| t.codec_params.codec != CODEC_TYPE_NULL)
        .ok_or("No audio track found")?;

    let codec_params = track.codec_params.clone();
    let decoder = symphonia::default::get_codecs()
        .make(&codec_params, &DecoderOptions::default())
        .map_err(|e| format!("Decoder creation failed: {}", e))?;

    let rate = codec_params.sample_rate.unwrap_or(24000);
    let channels = codec_params.channels.map(|c| c.count()).unwrap_or(1);
    Ok((decoder, rate, channels))
}

/// Incremental MP3 state: one persistent codec decoder (preserving Layer III
/// bit-reservoir continuity) fed with self-framed packets. Keeps the whole
/// received byte buffer so a framing failure can replay through the fallback
/// path without losing audio.
struct Mp3IncrementalState {
    decoder: Box<dyn Decoder>,
    channels: usize,
    resampler: IncrementalResampler,
    /// All encoded bytes received (consumed frames included).
    buf: Vec<u8>,
    /// Frame scan cursor: frames before it are already decoded.
    scan_pos: usize,
    /// Bytes skipped by sync scanning (junk/ID3), reset on each found frame.
    junk: usize,
    ts: u64,
    /// PCM bytes already emitted (aligns fallback replay on degrade).
    emitted_pcm_bytes: usize,
    finished: bool,
    degrade: bool,
}

enum Mp3Step {
    Output(Vec<u8>),
    Degrade,
}

impl Mp3IncrementalState {
    /// Decode every complete frame currently available; returns the new PCM
    /// bytes, or Degrade when framing is hopeless and the caller should replay
    /// the buffer through the whole-buffer path.
    fn decode_available(&mut self) -> Result<Mp3Step, String> {
        let mut produced: Vec<i16> = Vec::new();

        // Skip a leading ID3v2 tag once before frame scanning.
        if self.scan_pos == 0 && self.junk == 0 {
            if let Some(total) = id3v2_total_len(&self.buf) {
                if total <= self.buf.len() {
                    self.scan_pos = total;
                } else {
                    return Ok(Mp3Step::Output(Vec::new())); // wait for the full tag
                }
            }
        }

        loop {
            match next_mp3_frame(&self.buf, self.scan_pos, &mut self.junk) {
                Some((start, info)) => {
                    let packet = Packet::new_from_slice(
                        0,
                        self.ts,
                        info.samples,
                        &self.buf[start..start + info.len],
                    );
                    self.ts += info.samples;
                    self.scan_pos = start + info.len;
                    self.junk = 0;
                    decode_packet_into(&packet, self.decoder.as_mut(), self.channels, &mut produced);
                }
                None => {
                    if self.junk >= MP3_SYNC_GIVE_UP {
                        self.degrade = true;
                    }
                    break;
                }
            }
        }

        if self.degrade {
            return Ok(Mp3Step::Degrade);
        }

        self.resampler.push(&produced);
        if self.finished {
            self.resampler.flush();
        }
        let samples = std::mem::take(&mut self.resampler.out);

        let mut pcm_bytes = Vec::with_capacity(samples.len() * 2);
        for sample in &samples {
            pcm_bytes.extend_from_slice(&sample.to_le_bytes());
        }
        self.emitted_pcm_bytes += pcm_bytes.len();
        Ok(Mp3Step::Output(pcm_bytes))
    }
}

/// Streaming audio decoder.
///
/// MP3 (the production TTS format) decodes incrementally: each encoded byte is
/// framed and decoded exactly once, so cost grows linearly with stream length.
/// PCM passes fed bytes through. Other formats keep the previous
/// accumulate-and-redecode behavior (cost quadratic in stream length,
/// unchanged), which is also the replay path when MP3 framing gives up.
pub struct StreamingDecoder {
    format: String,
    target_sample_rate: u32,
    mode: Mode,
}

enum Mode {
    /// "pcm": raw passthrough, decode_available drains what was fed.
    Passthrough(Vec<u8>),
    /// MP3 incremental path. Inner None until the first successful probe; fed
    /// bytes wait in `stashed` until then.
    Mp3 {
        stashed: Vec<u8>,
        state: Option<Mp3IncrementalState>,
        /// Set by finish(); survives before the first successful probe so a
        /// short finished stream still surfaces its probe error.
        finished: bool,
    },
    /// Whole-buffer re-decode per call (previous behavior, non-MP3 formats).
    Fallback {
        buffer: Vec<u8>,
        emitted_pcm_bytes: usize,
    },
}

impl StreamingDecoder {
    pub fn new(format: &str, target_sample_rate: u32) -> Self {
        let mode = if format == "pcm" {
            Mode::Passthrough(Vec::new())
        } else if format == "mp3" {
            Mode::Mp3 {
                stashed: Vec::new(),
                state: None,
                finished: false,
            }
        } else {
            Mode::Fallback {
                buffer: Vec::new(),
                emitted_pcm_bytes: 0,
            }
        };
        Self {
            format: format.to_string(),
            target_sample_rate,
            mode,
        }
    }

    /// Feed encoded audio data into the buffer.
    pub fn feed(&mut self, chunk: &[u8]) {
        match &mut self.mode {
            Mode::Passthrough(pending) => pending.extend_from_slice(chunk),
            Mode::Mp3 { stashed, state, .. } => match state {
                Some(st) => st.buf.extend_from_slice(chunk),
                None => stashed.extend_from_slice(chunk),
            },
            Mode::Fallback { buffer, .. } => buffer.extend_from_slice(chunk),
        }
    }

    /// Mark the stream as finished (no more feeds) and drain remaining audio.
    pub fn finish(&mut self) -> Result<Vec<u8>, String> {
        match &mut self.mode {
            Mode::Passthrough(_) => {}
            Mode::Mp3 { state, finished, .. } => {
                *finished = true;
                if let Some(st) = state {
                    st.finished = true;
                }
            }
            Mode::Fallback { .. } => {}
        }
        self.decode_available()
    }

    /// Decode whatever is currently decodable and return the newly available
    /// PCM bytes.
    pub fn decode_available(&mut self) -> Result<Vec<u8>, String> {
        // MP3 path may hand the stream over to Fallback on framing failure.
        if let Mode::Mp3 {
            stashed,
            state,
            finished,
        } = &mut self.mode
        {
            if state.is_none() {
                if stashed.is_empty() {
                    return Ok(Vec::new());
                }
                match probe_mp3_decoder(stashed) {
                    Ok((decoder, rate, channels)) => {
                        *state = Some(Mp3IncrementalState {
                            decoder,
                            channels,
                            resampler: IncrementalResampler::new(rate, self.target_sample_rate),
                            buf: std::mem::take(stashed),
                            scan_pos: 0,
                            junk: 0,
                            ts: 0,
                            emitted_pcm_bytes: 0,
                            finished: *finished,
                            degrade: false,
                        });
                    }
                    Err(e) => {
                        // Not enough data for the probe yet: wait for more
                        // chunks instead of erroring. Only a finished (drained)
                        // stream that still cannot be probed is a real error,
                        // matching the one-shot decoder's failure mode.
                        if !*finished {
                            return Ok(Vec::new());
                        }
                        return Err(e);
                    }
                }
            }

            let st = state.as_mut().unwrap();
            match st.decode_available()? {
                Mp3Step::Output(pcm) => return Ok(pcm),
                Mp3Step::Degrade => {
                    let st = state.take().unwrap();
                    self.mode = Mode::Fallback {
                        buffer: st.buf,
                        emitted_pcm_bytes: st.emitted_pcm_bytes,
                    };
                }
            }
        }

        match &mut self.mode {
            Mode::Passthrough(pending) => Ok(std::mem::take(pending)),
            Mode::Fallback {
                buffer,
                emitted_pcm_bytes,
            } => {
                if buffer.is_empty() {
                    return Ok(Vec::new());
                }
                let pcm = decode_audio_to_pcm(buffer, &self.format, self.target_sample_rate)?;
                if pcm.len() <= *emitted_pcm_bytes {
                    return Ok(Vec::new());
                }
                let new_pcm = pcm[*emitted_pcm_bytes..].to_vec();
                *emitted_pcm_bytes = pcm.len();
                Ok(new_pcm)
            }
            Mode::Mp3 { .. } => unreachable!("mp3 handled above"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    /// Real TTS notification sound from core/assets/sounds.
    const FIXTURE_MP3: &[u8] = include_bytes!("../../../core/assets/sounds/tts_notify.mp3");

    /// Feed `data` through a StreamingDecoder in small chunks, draining after
    /// each chunk, then finish. Returns the concatenated incremental PCM.
    fn decode_incrementally(
        format: &str,
        data: &[u8],
        chunk_size: usize,
        target_rate: u32,
    ) -> Result<Vec<u8>, String> {
        let mut dec = StreamingDecoder::new(format, target_rate);
        let mut out = Vec::new();
        for chunk in data.chunks(chunk_size) {
            dec.feed(chunk);
            out.extend_from_slice(&dec.decode_available()?);
        }
        out.extend_from_slice(&dec.finish()?);
        Ok(out)
    }

    /// The pre-refactor behavior simulated with the fallback mode: feed the
    /// whole buffer, re-decode everything, keep only the new tail.
    fn decode_fallback_style(
        format: &str,
        data: &[u8],
        chunk_size: usize,
        target_rate: u32,
    ) -> Result<Vec<u8>, String> {
        let mut buffer: Vec<u8> = Vec::new();
        let mut emitted = 0usize;
        let mut out = Vec::new();
        for chunk in data.chunks(chunk_size) {
            buffer.extend_from_slice(chunk);
            let pcm = decode_audio_to_pcm(&buffer, format, target_rate)?;
            if pcm.len() > emitted {
                out.extend_from_slice(&pcm[emitted..]);
                emitted = pcm.len();
            }
        }
        Ok(out)
    }

    #[test]
    fn mp3_incremental_matches_oneshot_small_chunks() {
        let oneshot = decode_audio_to_pcm(FIXTURE_MP3, "mp3", 24000).unwrap();
        for chunk in [77usize, 1024, 4096] {
            let incr = decode_incrementally("mp3", FIXTURE_MP3, chunk, 24000).unwrap();
            assert_eq!(
                incr, oneshot,
                "incremental (chunk={}) must match one-shot decode byte-for-byte",
                chunk
            );
        }
    }

    #[test]
    fn mp3_incremental_matches_oneshot_leading_junk_and_id3() {
        // Leading junk before the stream, plus junk between two streams.
        // (FIXTURE_MP3 itself starts with a real ID3v2 tag, covering the
        // skip-the-tag path; a hand-made size=0 ID3 is rejected by symphonia
        // itself, so it is not a valid test vector.)
        let mut tagged = vec![0x00, 0x12, 0x34, 0x56];
        tagged.extend_from_slice(FIXTURE_MP3);
        tagged.extend_from_slice(&[0x00, 0x00, 0x55]);
        tagged.extend_from_slice(FIXTURE_MP3);

        let oneshot = decode_audio_to_pcm(&tagged, "mp3", 24000).unwrap();
        let incr = decode_incrementally("mp3", &tagged, 333, 24000).unwrap();
        assert_eq!(incr, oneshot);
    }

    #[test]
    fn mp3_incremental_matches_oneshot_long_stream() {
        // Simulate a long streaming answer: 40 back-to-back MP3 streams.
        let mut long = Vec::with_capacity(FIXTURE_MP3.len() * 40);
        for _ in 0..40 {
            long.extend_from_slice(FIXTURE_MP3);
        }
        let oneshot = decode_audio_to_pcm(&long, "mp3", 24000).unwrap();
        let incr = decode_incrementally("mp3", &long, 8192, 24000).unwrap();
        assert_eq!(incr, oneshot, "long-stream incremental must match one-shot");
    }

    #[test]
    fn mp3_incremental_cost_is_linear_not_quadratic() {
        // Timing comparison against the pre-refactor behavior (whole-buffer
        // re-decode per chunk), measured at two stream lengths: the
        // incremental/fallback time ratio must widen as the stream grows
        // (linear vs quadratic cost).
        //
        // Correctness is asserted against the one-shot decode, NOT against
        // the fallback style: the pre-refactor incremental tail-slicing was
        // observed to drift from the final one-shot decode on long streams
        // (truncated-stream frame alignment changes as data grows), while the
        // incremental path here matches one-shot byte-for-byte.
        let evaluate = |copies: usize| -> (f64, f64) {
            let mut long = Vec::with_capacity(FIXTURE_MP3.len() * copies);
            for _ in 0..copies {
                long.extend_from_slice(FIXTURE_MP3);
            }
            let oneshot = decode_audio_to_pcm(&long, "mp3", 24000).unwrap();

            let t_incr = Instant::now();
            let incr = decode_incrementally("mp3", &long, 8192, 24000).unwrap();
            let incr_ms = t_incr.elapsed().as_secs_f64() * 1000.0;

            let t_fb = Instant::now();
            let _fb = decode_fallback_style("mp3", &long, 8192, 24000).unwrap();
            let fb_ms = t_fb.elapsed().as_secs_f64() * 1000.0;

            assert_eq!(incr, oneshot, "incremental must match one-shot decode");
            (incr_ms, fb_ms)
        };

        let (incr_small, fb_small) = evaluate(4);
        let (incr_large, fb_large) = evaluate(20);

        println!(
            "decode cost: 4 copies incr={:.1}ms fallback={:.1}ms (x{:.1}); 20 copies incr={:.1}ms fallback={:.1}ms (x{:.1})",
            incr_small,
            fb_small,
            fb_small / incr_small.max(1e-6),
            incr_large,
            fb_large,
            fb_large / incr_large.max(1e-6),
        );

        // Hard assertions: incremental stays near-linear (5x data should stay
        // well below 5x the fallback's growth), fallback degrades superlinearly.
        let incr_growth = incr_large / incr_small.max(1e-6);
        let fb_growth = fb_large / fb_small.max(1e-6);
        assert!(
            incr_growth < fb_growth,
            "incremental growth {:.2}x should beat fallback growth {:.2}x",
            incr_growth,
            fb_growth
        );
        assert!(
            incr_large < fb_large,
            "incremental ({:.1}ms) must be faster than fallback ({:.1}ms) on long streams",
            incr_large,
            fb_large
        );
    }

    #[test]
    fn incremental_resampler_matches_batch_resample() {
        // Chirp-ish deterministic signal, arbitrary rates.
        let samples: Vec<i16> = (0..10_000)
            .map(|i| ((i as f64 * 0.05).sin() * 8000.0) as i16)
            .collect();
        for (from, to) in [(24000u32, 16000u32), (16000, 24000), (22050, 24000)] {
            let batch = resample(&samples, from, to);
            let mut inc = IncrementalResampler::new(from, to);
            let mut incremental = Vec::new();
            for seg in samples.chunks(97) {
                inc.push(seg);
                incremental.append(&mut inc.out);
            }
            inc.flush();
            incremental.append(&mut inc.out);
            assert_eq!(
                incremental, batch,
                "incremental resample {}->{} must match batch",
                from, to
            );
        }
    }

    #[test]
    fn pcm_passthrough_drains_fed_bytes() {
        let mut dec = StreamingDecoder::new("pcm", 24000);
        dec.feed(b"hello ");
        assert_eq!(dec.decode_available().unwrap(), b"hello ".to_vec());
        dec.feed(b"world");
        assert_eq!(dec.finish().unwrap(), b"world".to_vec());
    }
}
