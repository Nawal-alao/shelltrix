//! Turns raw sync events into the kinds shelltrix's UI actually consumes.
//!
//! This exists as its own module, taking plain JSON rather than a matrix-sdk
//! type, for two reasons. It is testable without a homeserver — the reason
//! `stream::Queue` is separated the same way — and it is the one place that
//! knows Matrix's payload shapes, so `stream.rs` stays about plumbing.
//!
//! The classification is not cosmetic. `m.reaction` is an `m.room.message`
//! with an empty body, so anything that reads messages without classifying
//! renders reactions as empty bubbles. matrix-nio kept them apart for us by
//! giving them their own event class; here the split has to be explicit.

use pyo3::prelude::*;
use serde_json::{Map, Value};

/// A text message.
pub const KIND_MESSAGE: &str = "message";
/// An image message, with its media URL already resolved.
pub const KIND_IMAGE: &str = "image";
/// An emoji annotation on another message.
pub const KIND_REACTION: &str = "reaction";
/// Who is currently typing. Ephemeral: never recorded in the timeline.
pub const KIND_TYPING: &str = "typing";
/// An invitation to a room we have not joined.
pub const KIND_INVITE: &str = "invite";

/// One event, resolved into the shape the UI wants.
///
/// Flat and tagged, like `/sync` itself: a message fills five of the fields
/// and leaves the rest empty. Reshaping into the Python dataclasses of
/// `shelltrix.events` happens on the Python side, which is the layer that
/// knows about `ImageEvent` and friends.
// The `#[pyclass]` sits here rather than on a parallel struct in `stream.rs`:
// a copy of these thirteen fields would be a translation to keep in sync, and
// the first field added to only one side would show up as an empty value in
// Python instead of an error.
#[pyclass(frozen, get_all, skip_from_py_object, module = "shelltrix_core")]
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Event {
    pub kind: String,
    pub room_id: String,
    pub sender: String,
    pub origin_server_ts: u64,
    pub event_id: String,
    pub msgtype: String,
    pub body: String,
    pub mentions: bool,
    /// Images: the resolved media URL.
    pub url: String,
    /// Reactions: the annotated event.
    pub target: String,
    /// Reactions: the emoji.
    pub key: String,
    /// Typing: the user IDs currently typing.
    pub users: Vec<String>,
    /// The raw event JSON, so the UI can read relations the dataclasses do
    /// not model (`m.in_reply_to` on a reply).
    pub source: String,
}

#[pymethods]
impl Event {
    fn __repr__(&self) -> String {
        format!(
            "Event(kind={:?}, room_id={:?}, event_id={:?})",
            self.kind, self.room_id, self.event_id
        )
    }
}

/// Classifies one timeline event: a message, an image, or a reaction.
pub fn timeline_event(room_id: &str, event: &Value) -> Option<Event> {
    let event = event.as_object()?;
    match event.get("type").and_then(Value::as_str)? {
        "m.room.message" => message(room_id, event),
        _ => None,
    }
}

/// Classifies one ephemeral room event. Today only typing reaches the UI.
pub fn ephemeral_event(room_id: &str, event: &Value) -> Option<Event> {
    let event = event.as_object()?;
    match event.get("type").and_then(Value::as_str)? {
        "m.typing" => Some(typing(room_id, event)),
        _ => None,
    }
}

/// Finds the invitation addressed to `me` in a room's stripped state.
///
/// An invite room's state is a handful of stripped events, and the one that
/// matters is the `m.room.member` whose `state_key` is us. Returning the
/// inviter is what the invite popup needs, and it is only in that event.
pub fn invite(room_id: &str, stripped: &[Value], me: &str) -> Option<Event> {
    for event in stripped {
        let Some(object) = event.as_object() else {
            continue;
        };
        if object.get("type").and_then(Value::as_str) != Some("m.room.member") {
            continue;
        }
        if object.get("state_key").and_then(Value::as_str) != Some(me) {
            continue;
        }
        let content = object.get("content");
        let membership = content
            .and_then(|c| c.get("membership"))
            .and_then(Value::as_str);
        if membership != Some("invite") {
            continue;
        }
        return Some(Event {
            kind: KIND_INVITE.to_owned(),
            room_id: room_id.to_owned(),
            sender: text(object, "sender"),
            source: Value::Object(object.clone()).to_string(),
            ..Event::default()
        });
    }
    None
}

fn message(room_id: &str, event: &Map<String, Value>) -> Option<Event> {
    let content = event.get("content")?.as_object()?;
    let msgtype = content
        .get("msgtype")
        .and_then(Value::as_str)
        .unwrap_or("m.text")
        .to_owned();

    let mut out = Event {
        room_id: room_id.to_owned(),
        sender: text(event, "sender"),
        origin_server_ts: event
            .get("origin_server_ts")
            .and_then(Value::as_u64)
            .unwrap_or_default(),
        event_id: text(event, "event_id"),
        mentions: content
            .get("m.mentions")
            .and_then(|m| m.get("user_ids"))
            .and_then(Value::as_array)
            .is_some_and(|ids| !ids.is_empty()),
        source: Value::Object(event.clone()).to_string(),
        ..Event::default()
    };

    // A reaction IS an m.room.message, so it must be recognized before the
    // msgtype is, or it arrives as an empty message.
    if let Some(annotation) = annotation(content) {
        out.kind = KIND_REACTION.to_owned();
        out.target = text(annotation, "event_id");
        out.key = text(annotation, "key");
        return Some(out);
    }

    out.body = content
        .get("body")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    if msgtype == "m.image" {
        out.kind = KIND_IMAGE.to_owned();
        out.url = image_url(content);
        // The body is a file name; the UI labels an image with it, or says
        // "image" when the server sent none.
        if out.body.is_empty() {
            out.body = "image".to_owned();
        }
    } else {
        out.kind = KIND_MESSAGE.to_owned();
        out.msgtype = msgtype;
    }
    Some(out)
}

fn typing(room_id: &str, event: &Map<String, Value>) -> Event {
    let users = event
        .get("content")
        .and_then(|c| c.get("user_ids"))
        .and_then(Value::as_array)
        .map(|ids| {
            ids.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    Event {
        kind: KIND_TYPING.to_owned(),
        room_id: room_id.to_owned(),
        users,
        source: Value::Object(event.clone()).to_string(),
        ..Event::default()
    }
}

/// The `m.relates_to` block of an emoji annotation, if this is one.
fn annotation(content: &Map<String, Value>) -> Option<&Map<String, Value>> {
    let relates = content.get("m.relates_to")?.as_object()?;
    if relates.get("rel_type").and_then(Value::as_str) != Some("m.annotation") {
        return None;
    }
    Some(relates)
}

/// Resolves the media URL of an image event, whichever shape was used.
///
/// Matrix v3 serves `url`; older servers and encrypted uploads nest it under
/// `file`. Resolving here means the UI never sees that difference — the same
/// rule the Python transport applied before it, kept because the wire still
/// sends both.
fn image_url(content: &Map<String, Value>) -> String {
    content
        .get("url")
        .and_then(Value::as_str)
        .or_else(|| {
            content
                .get("file")
                .and_then(|f| f.get("url"))
                .and_then(Value::as_str)
        })
        .unwrap_or_default()
        .to_owned()
}

fn text(object: &Map<String, Value>, key: &str) -> String {
    object
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn classify(event: Value) -> Option<Event> {
        timeline_event("!r:hs", &event)
    }

    #[test]
    fn a_plain_text_message() {
        let got = classify(json!({
            "type": "m.room.message",
            "sender": "@alice:hs",
            "event_id": "$1",
            "origin_server_ts": 1_700_000_000_000u64,
            "content": {"msgtype": "m.text", "body": "hello"},
        }))
        .unwrap();
        assert_eq!(got.kind, KIND_MESSAGE);
        assert_eq!(got.body, "hello");
        assert_eq!(got.msgtype, "m.text");
        assert_eq!(got.room_id, "!r:hs");
        assert!(!got.mentions);
    }

    /// The bug this module exists to prevent: a reaction is a message with an
    /// empty body, so an unclassified read renders an empty bubble.
    #[test]
    fn a_reaction_is_not_a_message() {
        let got = classify(json!({
            "type": "m.room.message",
            "sender": "@bob:hs",
            "event_id": "$2",
            "content": {"msgtype": "m.text", "body": "", "m.relates_to": {
                "rel_type": "m.annotation", "event_id": "$1", "key": "\u{1f44d}",
            }},
        }))
        .unwrap();
        assert_eq!(got.kind, KIND_REACTION);
        assert_eq!(got.target, "$1");
        assert_eq!(got.key, "\u{1f44d}");
        assert!(got.body.is_empty());
    }

    /// A reply also carries `m.relates_to`, but it is not an annotation, and
    /// it is a message the user expects to see.
    #[test]
    fn a_reply_is_still_a_message() {
        let got = classify(json!({
            "type": "m.room.message",
            "event_id": "$3",
            "content": {"msgtype": "m.text", "body": "answering", "m.relates_to": {
                "rel_type": "m.thread", "event_id": "$1", "is_falling_back": true,
            }},
        }))
        .unwrap();
        assert_eq!(got.kind, KIND_MESSAGE);
        assert_eq!(got.body, "answering");
    }

    #[test]
    fn an_image_keeps_its_v3_url() {
        let got = classify(json!({
            "type": "m.room.message",
            "event_id": "$4",
            "content": {"msgtype": "m.image", "body": "cat.png",
                        "url": "mxc://hs/abc", "info": {"mimetype": "image/png"}},
        }))
        .unwrap();
        assert_eq!(got.kind, KIND_IMAGE);
        assert_eq!(got.url, "mxc://hs/abc");
        assert_eq!(got.body, "cat.png");
    }

    /// An encrypted upload nests the URL under `file`. That is the shape the
    /// Python transport resolved too, and getting it wrong yields a broken
    /// image rather than an error.
    #[test]
    fn an_encrypted_image_keeps_its_nested_url() {
        let got = classify(json!({
            "type": "m.room.message",
            "event_id": "$5",
            "content": {"msgtype": "m.image", "body": "secret.png",
                        "file": {"url": "mxc://hs/xyz", "key": {"k": "..."}}},
        }))
        .unwrap();
        assert_eq!(got.url, "mxc://hs/xyz");
    }

    #[test]
    fn an_image_without_a_file_name_still_has_a_label() {
        let got = classify(json!({
            "type": "m.room.message",
            "event_id": "$6",
            "content": {"msgtype": "m.image", "url": "mxc://hs/abc"},
        }))
        .unwrap();
        assert_eq!(got.body, "image");
    }

    #[test]
    fn a_mention_is_noticed() {
        let got = classify(json!({
            "type": "m.room.message",
            "event_id": "$7",
            "content": {"msgtype": "m.text", "body": "hey",
                        "m.mentions": {"user_ids": ["@me:hs"]}},
        }))
        .unwrap();
        assert!(got.mentions);
    }

    #[test]
    fn typing_carries_who_is_typing() {
        let got = ephemeral_event(
            "!r:hs",
            &json!({"type": "m.typing", "content": {"user_ids": ["@bob:hs", "@carol:hs"]}}),
        )
        .unwrap();
        assert_eq!(got.kind, KIND_TYPING);
        assert_eq!(got.users, ["@bob:hs", "@carol:hs"]);
    }

    /// Typing carries a full list every time, so an empty one means "stopped"
    /// and must reach the UI as an empty list rather than being dropped.
    #[test]
    fn a_typing_that_stopped_is_still_an_event() {
        let got = ephemeral_event(
            "!r:hs",
            &json!({"type": "m.typing", "content": {"user_ids": []}}),
        )
        .unwrap();
        assert_eq!(got.users, Vec::<String>::new());
    }

    #[test]
    fn an_invite_names_who_asked() {
        let got = invite(
            "!r:hs",
            &[
                json!({"type": "m.room.name", "content": {"name": "plans"}}),
                json!({"type": "m.room.member", "sender": "@bob:hs", "state_key": "@me:hs",
                       "content": {"membership": "invite"}}),
            ],
            "@me:hs",
        )
        .unwrap();
        assert_eq!(got.kind, KIND_INVITE);
        assert_eq!(got.sender, "@bob:hs");
    }

    /// A room can also be invited with the member event addressed to someone
    /// else, in a stripped state that includes several members.
    #[test]
    fn an_invite_addressed_to_someone_else_is_ignored() {
        let got = invite(
            "!r:hs",
            &[json!({"type": "m.room.member", "sender": "@bob:hs",
                     "state_key": "@them:hs", "content": {"membership": "invite"}})],
            "@me:hs",
        );
        assert!(got.is_none());
    }

    /// A second invite for the same room arrives as a membership change, not
    /// an invite; reporting it again would re-open a dialog the user dismissed.
    #[test]
    fn a_membership_change_is_not_an_invite() {
        let got = invite(
            "!r:hs",
            &[
                json!({"type": "m.room.member", "sender": "@bob:hs", "state_key": "@me:hs",
                     "content": {"membership": "join"}}),
            ],
            "@me:hs",
        );
        assert!(got.is_none());
    }

    #[test]
    fn unrelated_events_are_dropped() {
        assert!(classify(json!({"type": "m.room.member",
                                "content": {"membership": "join"}}))
        .is_none());
        assert!(ephemeral_event("!r:hs", &json!({"type": "m.receipt", "content": {}})).is_none());
    }

    /// The UI reads relations from the raw event; losing it would silently
    /// break reply rendering, so it travels with every timeline event.
    #[test]
    fn the_raw_event_travels_with_the_message() {
        let got = classify(json!({
            "type": "m.room.message",
            "event_id": "$8",
            "content": {"msgtype": "m.text", "body": "x",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$1"}},
        }))
        .unwrap();
        let source: Value = serde_json::from_str(&got.source).unwrap();
        assert_eq!(
            source["content"]["m.relates_to"]["event_id"],
            json!("$1"),
            "the reply target must survive to Python"
        );
    }
}
