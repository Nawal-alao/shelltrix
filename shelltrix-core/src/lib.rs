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

use pyo3::exceptions::PyValueError;
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
            if event.get("type").and_then(|t| t.as_str()) != Some("m.room.message") {
                continue;
            }
            let content = match event.get("content") {
                Some(content) => content,
                None => continue,
            };
            let msgtype = content
                .get("msgtype")
                .and_then(|m| m.as_str())
                .unwrap_or("m.text");
            out.push(SyncMessage {
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
            });
        }
    }
    Ok(out)
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
    m.add_class::<SyncMessage>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

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
