use crate::python::PythonManager;
use open_xiaoai::base::{AppError, VERSION};
use open_xiaoai::services::audio::config::AudioConfig;
use open_xiaoai::services::connect::data::{Event, Request, Response, Stream};
use open_xiaoai::services::connect::handler::MessageHandler;
use open_xiaoai::services::connect::message::{MessageManager, WsStream};
use open_xiaoai::services::connect::rpc::RPC;
use open_xiaoai::services::speaker::SpeakerManager;
use open_xiaoai::utils::task::TaskManager;
use pyo3::types::PyBytes;
use pyo3::types::PyString;
use pyo3::Python;
use serde_json::json;
use std::env;
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::accept_hdr_async;
use tokio_tungstenite::tungstenite::handshake::server::ErrorResponse;

pub struct AppServer;

/// 401 握手拒绝（不携带 token / 不泄露期望值）。
fn unauthorized(body: &str) -> ErrorResponse {
    tokio_tungstenite::tungstenite::http::Response::builder()
        .status(401)
        .body(Some(body.to_string()))
        .unwrap()
}

/// 解析 `Authorization: Bearer <token>`；不合规返回 None（与 Python 侧同语义）。
fn bearer_token(header: &str) -> Option<&str> {
    let mut parts = header.split_whitespace();
    let scheme = parts.next()?;
    let token = parts.next()?;
    if parts.next().is_some() || !scheme.eq_ignore_ascii_case("bearer") {
        return None;
    }
    Some(token)
}

/// 常时字符串比较：避免逐字节提前返回泄露 token 前缀。
fn constant_time_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// Check if audio input is enabled via environment variable.
/// Supports: "true"/"false", "1"/"0", "yes"/"no", etc.
/// Defaults to true if not set or invalid.
fn is_audio_input_enabled() -> bool {
    match env::var("AUDIO_INPUT_ENABLE") {
        Ok(val) => {
            let val = val.trim().to_lowercase();
            matches!(val.as_str(), "true" | "1" | "yes" | "on")
        }
        Err(_) => true,
    }
}

async fn test() -> Result<(), AppError> {
    SpeakerManager::play_text("已连接").await?;

    // Only start recording if audio input is enabled
    if is_audio_input_enabled() {
        let _ = RPC::instance()
            .call_remote(
                "start_recording",
                Some(json!(AudioConfig {
                    pcm: "noop".into(),
                    channels: 1,
                    bits_per_sample: 16,
                    sample_rate: 16000,
                    period_size: 1440 / 4,
                    buffer_size: 1440,
                })),
                None,
            )
            .await;
    }

    // aplay is started lazily by ensure_player_ready() on first audio send,
    // avoiding empty-buffer underruns from idling aplay processes.

    Ok(())
}

impl AppServer {
    /// 期望的 client token（去空白）。未配置 → 空串。
    fn expected_token() -> String {
        env::var("OPEN_XIAOAI_TOKEN")
            .map(|v| v.trim().to_string())
            .unwrap_or_default()
    }

    /// 完成 WebSocket 握手。**失败关闭**：`OPEN_XIAOAI_TOKEN` 未配置时
    /// 一律 401 拒绝（不再退回无鉴权连接），配置正确 token 的 client 可连。
    pub async fn connect(stream: TcpStream) -> Result<WsStream, AppError> {
        use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};

        let expected_token = AppServer::expected_token();
        let ws_stream = accept_hdr_async(stream, move |req: &Request, response: Response| {
            if expected_token.is_empty() {
                // 未配置：拒绝且提示如何配置（不含任何 token 值）
                return Err(unauthorized("Unauthorized: OPEN_XIAOAI_TOKEN not configured"));
            }
            let auth = req
                .headers()
                .get("Authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            let provided = bearer_token(auth).unwrap_or("");
            if !constant_time_eq(provided, &expected_token) {
                return Err(unauthorized("Unauthorized"));
            }
            Ok(response)
        })
        .await?;
        Ok(WsStream::Server(ws_stream))
    }

    pub async fn run() {
        let addr = "0.0.0.0:4399";
        if AppServer::expected_token().is_empty() {
            crate::pylog!(
                "[AppServer] ⚠️ OPEN_XIAOAI_TOKEN 未配置：所有连接将被拒绝（fail closed）"
            );
        }
        let listener = TcpListener::bind(&addr)
            .await
            .expect(format!("[AppServer] ❌ 绑定地址失败: {}", &addr).as_str());
        crate::pylog!("[AppServer] ✅ 已启动: {:?}", addr);
        while let Ok((stream, addr)) = listener.accept().await {
            // 同一时刻只处理一个连接
            AppServer::handle_connection(stream, addr).await;
        }
    }

    async fn handle_connection(stream: TcpStream, addr: std::net::SocketAddr) {
        let ws_stream = match AppServer::connect(stream).await {
            Ok(ws_stream) => ws_stream,
            Err(e) => {
                let msg = e.to_string();
                if msg.contains("401") || msg.contains("Unauthorized") {
                    crate::pylog!(
                        "[AppServer] ❌ 鉴权失败（需带 Authorization: Bearer <OPEN_XIAOAI_TOKEN>）: {}",
                        addr
                    );
                } else {
                    crate::pylog!("[AppServer] ❌ 连接异常: {} ({})", addr, msg);
                }
                return;
            }
        };
        crate::pylog!("[AppServer] ✅ 已连接: {:?}", addr);
        AppServer::init(ws_stream).await;
        if let Err(e) = MessageManager::instance().process_messages().await {
            crate::pylog!("[AppServer] ❌ 消息处理异常: {}", e);
        }
        AppServer::dispose().await;
        crate::pylog!("[AppServer] ❌ 已断开连接");
    }

    async fn init(ws_stream: WsStream) {
        MessageManager::instance().init(ws_stream).await;
        MessageHandler::<Event>::instance()
            .set_handler(on_event)
            .await;
        MessageHandler::<Stream>::instance()
            .set_handler(on_stream)
            .await;

        let rpc = RPC::instance();
        rpc.add_command("get_version", get_version).await;

        let test = tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            let _ = test().await;
        });
        TaskManager::instance().add("test", test).await;
    }

    async fn dispose() {
        MessageManager::instance().dispose().await;
        TaskManager::instance().dispose("test").await;
    }
}

async fn get_version(_: Request) -> Result<Response, AppError> {
    let data = json!(VERSION.to_string());
    Ok(Response::from_data(data))
}

async fn on_stream(stream: Stream) -> Result<(), AppError> {
    let Stream { tag, bytes, .. } = stream;
    match tag.as_str() {
        "record" => {
            let data = Python::with_gil(|py| PyBytes::new(py, &bytes).into());
            PythonManager::instance().call_fn("on_input_data", Some(data))?;
        }
        _ => {}
    }
    Ok(())
}

async fn on_event(event: Event) -> Result<(), AppError> {
    let event_json = serde_json::to_string(&event)?;
    let data = Python::with_gil(|py| PyString::new(py, &event_json).into());
    PythonManager::instance().call_fn("on_event", Some(data))?;
    Ok(())
}
