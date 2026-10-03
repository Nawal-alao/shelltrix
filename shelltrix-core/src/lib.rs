//! Rust core of shelltrix.
//!
//! Boundary rules, from `docs/decisions/0001-rust-core.md`:
//!
//! - This crate owns the Matrix transport and the E2EE. Nothing else.
//! - The Python facade `shelltrix.matrix_client.ShelltrixClient` keeps its
//!   signatures, so the UI, the cache and the widgets never import from here
//!   directly. They go through the facade, which selects a backend.
//! - Every function here takes and returns plain data. No PyO3 types leak
//!   across the boundary, so the Rust side stays testable on its own and the
//!   Python side stays free to reshape what it receives.
//!
//! What lives here today is the first migrated slice: parsing a `/sync`
//! response into timeline messages. It was chosen over the crypto path
//! deliberately — it is the slice with no secret state, so it can be
//! migrated and validated without touching what must not break.

// matrix-sdk's futures are deeply nested — each async layer is wrapped by
// `tracing::instrument`, and the crypto path adds several more. Proving that
// the sync loop's future is `Send`, which `tokio::spawn` requires, blows past
// rustc's default recursion limit of 128. Raising it is the compiler's own
// suggested fix; there is no restructuring on our side that avoids it.
#![recursion_limit = "512"]

pub mod classify;
pub mod rooms;
pub mod runtime;
pub mod stream;
pub mod transport;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

/// One `m.room.message` of a `/sync` response.
#[pyclass(frozen, get_all, skip_from_py_object, module = "shelltrix_core")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncMessage {
    /// Full Matrix identifier of the author, `@alice:hs`.
    pub sender: String,
    /// Server timestamp, milliseconds since the epoch.
    pub origin_server_ts: u64,
    /// Server identifier, used for pagination and de-duplication.
    pub event_id: String,
    /// `m.text`, `m.emote`, `m.image`, … Empty for unknown types.
    pub msgtype: String,
    /// Message body, already unescaped by serde.
    pub body: String,
    /// True when the content carries a `m.mentions` block naming the user.
    pub mentions: bool,
}

#[pymethods]
impl SyncMessage {
    fn __repr__(&self) -> String {
        format!(
            "SyncMessage(event_id={:?}, sender={:?}, ts={})",
            self.event_id, self.sender, self.origin_server_ts
        )
    }
}

/// The fields of an `m.room.message` event, without the room it came from.
///
/// Shared by `parse_sync_messages` (which walks a whole payload) and the sync
/// stream (which is handed one event at a time), so both agree on what a
/// message is by construction rather than by two implementations staying in
/// sync by hand.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MessageFields {
    pub sender: String,
    pub origin_server_ts: u64,
    pub event_id: String,
    pub msgtype: String,
    pub body: String,
    pub mentions: bool,
}

/// Reads one `m.room.message` event, or None if it is not one.
///
/// Takes the raw event object, so both the `/sync` payload parser and the
/// stream can hand over whatever shape they have.
pub fn message_fields(event: &serde_json::Map<String, serde_json::Value>) -> Option<MessageFields> {
    if event.get("type").and_then(|t| t.as_str()) != Some("m.room.message") {
        return None;
    }
    let content = event.get("content")?;
    let msgtype = content
        .get("msgtype")
        .and_then(|m| m.as_str())
        .unwrap_or("m.text");
    Some(MessageFields {
        sender: event
            .get("sender")
            .and_then(|s| s.as_str())
            .unwrap_or_default()
            .to_owned(),
        origin_server_ts: event
            .get("origin_server_ts")
            .and_then(|t| t.as_u64())
            .unwrap_or_default(),
        event_id: event
            .get("event_id")
            .and_then(|e| e.as_str())
            .unwrap_or_default()
            .to_owned(),
        msgtype: msgtype.to_owned(),
        body: content
            .get("body")
            .and_then(|b| b.as_str())
            .unwrap_or_default()
            .to_owned(),
        mentions: content
            .get("m.mentions")
            .and_then(|m| m.get("user_ids"))
            .and_then(|u| u.as_array())
            .is_some_and(|ids| !ids.is_empty()),
    })
}

/// Extract every `m.room.message` of the joined rooms of a `/sync` payload.
///
/// Accepts the raw response bytes, as received, because decoding a `/sync` with
/// `json.loads` and walking it in Python is precisely the cost this slice
/// removes. Rooms the user left or was invited to are ignored: they carry no
/// timeline to render.
///
/// Errors:
/// - `ValueError` if the payload is not valid JSON, or has no `rooms` object.
///   The Python core raised `KeyError` in that case; the facade keeps the
///   existing error handling, so this is a behaviour change confined to the
///   boundary.
#[pyfunction]
#[pyo3(text_signature = "(payload: bytes) -> list[SyncMessage]")]
fn parse_sync_messages(payload: &[u8]) -> PyResult<Vec<SyncMessage>> {
    let value: serde_json::Value = serde_json::from_slice(payload)
        .map_err(|e| PyValueError::new_err(format!("invalid /sync JSON: {e}")))?;

    let joined = value
        .get("rooms")
        .and_then(|rooms| rooms.get("join"))
        .and_then(|join| join.as_object())
        .ok_or_else(|| PyValueError::new_err("no `rooms.join` object in /sync"))?;

    let mut out = Vec::new();
    for room in joined.values() {
        let Some(events) = room
            .get("timeline")
            .and_then(|timeline| timeline.get("events"))
            .and_then(|events| events.as_array())
        else {
            continue;
        };
        for event in events {
            let Some(fields) = event.as_object().and_then(message_fields) else {
                continue;
            };
            out.push(SyncMessage {
                sender: fields.sender,
                origin_server_ts: fields.origin_server_ts,
                event_id: fields.event_id,
                msgtype: fields.msgtype,
                body: fields.body,
                mentions: fields.mentions,
            });
        }
    }
    Ok(out)
}

/// A `/sync` result, as Python sees it.
#[pyclass(frozen, get_all, skip_from_py_object, module = "shelltrix_core")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncSummary {
    /// `@alice:hs`, as the homeserver confirms it.
    pub user_id: String,
    /// The device this session authenticated as.
    pub device_id: String,
    /// Joined room identifiers, sorted.
    pub joined_rooms: Vec<String>,
}

#[pymethods]
impl SyncSummary {
    fn __repr__(&self) -> String {
        format!(
            "SyncSummary(user_id={:?}, device_id={:?}, rooms={})",
            self.user_id,
            self.device_id,
            self.joined_rooms.len()
        )
    }
}

/// Logs in to a homeserver and runs one `/sync`, then comes back.
///
/// This is the proof that the migration is mechanically possible from Python:
/// matrix-sdk's Tokio runs on its own thread (see `runtime`), the GIL is
/// released while it waits, and the result arrives as a plain pyclass.
///
/// Blocking, and meant to be called off the event loop: the facade runs it in
/// a worker task, as it already does for matrix-nio's network calls.
///
/// Errors:
/// - `RuntimeError` if the homeserver is unreachable, the credentials are
///   wrong, or `/sync` fails.
#[pyfunction]
#[pyo3(text_signature = "(homeserver: str, user: str, password: str) -> SyncSummary")]
fn login_and_sync(
    py: Python<'_>,
    homeserver: &str,
    user: &str,
    password: &str,
) -> PyResult<SyncSummary> {
    // `detach` (called `allow_threads` before PyO3 0.29) is not an
    // optimisation: holding the GIL here would freeze the whole Textual UI
    // for the duration of the network round-trip.
    let got =
        py.detach(|| runtime::block_on(transport::login_and_sync(homeserver, user, password)));
    let summary = got.map_err(PyRuntimeError::new_err)?;
    Ok(SyncSummary {
        user_id: summary.user_id,
        device_id: summary.device_id,
        joined_rooms: summary.joined_rooms,
    })
}

/// Starts the sync loop in the background and returns immediately.
///
/// The caller then drains it with [`next_event`]. This split exists because
/// matrix-sdk's sync never returns: a transport has to own the loop as a task
/// and hand events over one at a time.
///
/// Errors:
/// - `RuntimeError` if a sync is already running, the homeserver is
///   unreachable, or the credentials are refused.
#[pyfunction]
#[pyo3(text_signature = "(homeserver: str, user: str, password: str) -> SyncSummary")]
fn start_sync(
    py: Python<'_>,
    homeserver: &str,
    user: &str,
    password: &str,
) -> PyResult<SyncSummary> {
    let got = py.detach(|| runtime::block_on(stream::start(homeserver, user, password)));
    let summary = got.map_err(PyRuntimeError::new_err)?;
    Ok(SyncSummary {
        user_id: summary.user_id,
        device_id: summary.device_id,
        joined_rooms: summary.joined_rooms,
    })
}

/// Waits up to `timeout_ms` for the next event from the sync loop.
///
/// Returns `None` when the wait expires, which is the normal outcome in a quiet
/// room and not an error. Raises `RuntimeError` if no sync is running, or if
/// the loop ended — with the reason, so the UI can say why it went quiet.
///
/// The event is [`classify::Event`], not a struct of its own: the fields are
/// the classified ones, and a second copy here would be a translation to keep
/// in sync. The first field added to only one side would read as an empty
/// value in Python rather than as an error.
#[pyfunction]
#[pyo3(text_signature = "(timeout_ms: int) -> Event | None")]
fn next_event(py: Python<'_>, timeout_ms: u64) -> PyResult<Option<classify::Event>> {
    let got = py.detach(|| runtime::block_on(stream::next(timeout_ms)));
    let event = got.map_err(PyRuntimeError::new_err)?;
    Ok(event)
}

/// Stops the sync loop. Safe to call when none is running.
#[pyfunction]
fn stop_sync() {
    stream::stop();
}

/// Restores a saved session and starts the sync loop in the background.
///
/// The path the app takes on every run after the first: `Credentials` holds an
/// access token and a device id, not a password, so [`start_sync`] cannot be
/// called. Logging in again on each launch would create a new device per run
/// and eventually trip the homeserver's device limit.
///
/// Errors:
/// - `RuntimeError` if a sync is already running, the homeserver is
///   unreachable, or the token was refused.
#[pyfunction]
#[pyo3(
    text_signature = "(homeserver: str, user_id: str, device_id: str, access_token: str) -> SyncSummary"
)]
fn start_sync_with_token(
    py: Python<'_>,
    homeserver: &str,
    user_id: &str,
    device_id: &str,
    access_token: &str,
) -> PyResult<SyncSummary> {
    let got = py.detach(|| {
        runtime::block_on(stream::start_with_token(
            homeserver,
            user_id,
            device_id,
            access_token,
        ))
    });
    let summary = got.map_err(PyRuntimeError::new_err)?;
    Ok(SyncSummary {
        user_id: summary.user_id,
        device_id: summary.device_id,
        joined_rooms: summary.joined_rooms,
    })
}

/// The current room list, with display names resolved. Empty if no sync runs.
///
/// Safe to call as often as the UI likes: the snapshot is only recomputed when
/// a `/sync` has invalidated it.
#[pyfunction]
#[pyo3(text_signature = "() -> list[RoomInfo]")]
fn rooms_snapshot(py: Python<'_>) -> PyResult<Vec<rooms::RoomInfo>> {
    Ok(py.detach(|| runtime::block_on(stream::rooms())))
}

/// Whether at least one `/sync` response has been processed.
///
/// The UI waits for this before listing rooms, because a room only arrives with
/// a sync response — never at construction time.
#[pyfunction]
fn first_sync_done() -> bool {
    stream::first_sync_done()
}

/// Whether this build can decrypt encrypted rooms.
///
/// Not a promise but a warning: the app checks it before trusting a sync on the
/// Rust backend, because without a crypto store an encrypted room is not
/// reported as unreadable — it is reported as *empty*, which looks like a quiet
/// conversation rather than a broken client.
#[pyfunction]
fn supports_e2ee() -> bool {
    cfg!(feature = "e2e-encryption")
}

/// Version of the compiled core, distinct from the shelltrix app version.
#[pyfunction]
fn core_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn shelltrix_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_sync_messages, m)?)?;
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    m.add_function(wrap_pyfunction!(login_and_sync, m)?)?;
    m.add_function(wrap_pyfunction!(start_sync, m)?)?;
    m.add_function(wrap_pyfunction!(start_sync_with_token, m)?)?;
    m.add_function(wrap_pyfunction!(next_event, m)?)?;
    m.add_function(wrap_pyfunction!(stop_sync, m)?)?;
    m.add_function(wrap_pyfunction!(rooms_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(first_sync_done, m)?)?;
    m.add_function(wrap_pyfunction!(supports_e2ee, m)?)?;
    m.add_class::<SyncMessage>()?;
    m.add_class::<SyncSummary>()?;
    m.add_class::<classify::Event>()?;
    m.add_class::<rooms::RoomInfo>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_runtime_bridge_is_reachable_from_this_crate() {
        assert_eq!(runtime::block_on(async { 7 }), 7);
    }

    fn payload() -> &'static str {
        r#"{"rooms":{"join":{"!a:hs":{"timeline":{"events":[
            {"type":"m.room.message","event_id":"$1","sender":"@alice:hs",
             "origin_server_ts":1700000000000,
             "content":{"msgtype":"m.text","body":"hello"}},
            {"type":"m.room.member","event_id":"$2","sender":"@bob:hs",
             "origin_server_ts":1700000001000,"content":{"membership":"join"}},
            {"type":"m.room.message","event_id":"$3","sender":"@bob:hs",
             "origin_server_ts":1700000002000,
             "content":{"msgtype":"m.image","body":"f.png",
                        "m.mentions":{"user_ids":["@alice:hs"]}}}
        ]}}},"leave":{"!gone:hs":{"timeline":{"events":[
            {"type":"m.room.message","event_id":"$9","sender":"@eve:hs",
             "origin_server_ts":1700000003000,"content":{"msgtype":"m.text","body":"gone"}}
        ]}}}}}"#
    }

    #[test]
    fn keeps_only_message_events() {
        let got = parse_sync_messages(payload().as_bytes()).unwrap();
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].event_id, "$1");
        assert_eq!(got[0].body, "hello");
        assert!(!got[0].mentions);
    }

    #[test]
    fn reads_msgtype_and_mentions() {
        let got = parse_sync_messages(payload().as_bytes()).unwrap();
        assert_eq!(got[1].msgtype, "m.image");
        assert!(got[1].mentions, "a non-empty m.mentions block must be seen");
    }

    #[test]
    fn skips_left_rooms() {
        let got = parse_sync_messages(payload().as_bytes()).unwrap();
        assert!(
            got.iter().all(|m| m.event_id != "$9"),
            "a room the user left carries no timeline to render"
        );
    }

    #[test]
    fn accepts_an_empty_room_set() {
        let got = parse_sync_messages(br#"{"rooms":{"join":{}}}"#).unwrap();
        assert!(got.is_empty());
    }

    #[test]
    fn rejects_malformed_json() {
        assert!(parse_sync_messages(b"{not json").is_err());
    }

    #[test]
    fn rejects_a_payload_without_rooms() {
        assert!(parse_sync_messages(br#"{"next_batch":"s1"}"#).is_err());
    }

    #[test]
    fn tolerates_events_without_optional_fields() {
        let got = parse_sync_messages(
            br#"{"rooms":{"join":{"!a:hs":{"timeline":{"events":[
                {"type":"m.room.message","content":{}}]}}}}}"#,
        )
        .unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].msgtype, "m.text", "the default msgtype");
        assert_eq!(got[0].body, "");
        assert_eq!(got[0].origin_server_ts, 0);
    }
}
