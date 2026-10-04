// Sending: the first operation to cross the boundary the other way.
//
// `/sync` runs in the background and hands events to Python one at a time, so
// reading needed no entry point of its own. Sending is different in three ways
// that shape this module:
//
// - It is a request the user is waiting on, so it is a plain blocking call —
//   `lib.rs` detaches the GIL around it — rather than something that joins the
//   event queue.
// - It goes through the *same* client the loop syncs with. matrix-sdk's send
//   path reads the room out of the client's store, so a second client would not
//   know the room and could not send to it at all.
// - It can put plaintext where only ciphertext belongs. See `leaks_plaintext`.

use matrix_sdk::{Client, EncryptionState};
use ruma::RoomId;
use serde_json::Value;

use crate::stream;

/// Why a send did not happen.
///
/// Split rather than collapsed into one string because the caller has to treat
/// the cases differently: `Encrypted` is a *capability* the core does not have
/// yet and becomes `UnsupportedOperation` in Python, while the others are real
/// failures the user should be told about.
#[derive(Debug)]
pub enum SendError {
    /// No sync is running, so there is no client to send with.
    NotSyncing,
    /// `room_id` is not a Matrix room id at all. A programming error upstream:
    /// the app only ever holds ids that came off a `/sync`.
    NotARoom(String),
    /// The room is not in the sync the core holds — never joined, left, or a
    /// typo. matrix-sdk can only send to a room its store knows.
    NoRoom(String),
    /// The room is encrypted and this build has no Olm machine. See
    /// [`leaks_plaintext`]: nothing was sent.
    Encrypted(String),
    /// The homeserver or matrix-sdk refused.
    Refused(String),
}

impl std::fmt::Display for SendError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotSyncing => {
                write!(f, "no sync is running, so there is nothing to send with")
            }
            Self::NotARoom(given) => write!(f, "{given:?} is not a Matrix room id"),
            Self::NoRoom(room) => write!(f, "the core has not synced {room}"),
            Self::Encrypted(room) => write!(
                f,
                "{room} is encrypted and this core cannot encrypt what it sends yet"
            ),
            Self::Refused(why) => write!(f, "the homeserver refused the message: {why}"),
        }
    }
}

/// Posts one message-like event through the running sync's client.
///
/// The thin wrapper the binding calls: it takes the client the sync loop is
/// using, because a client of its own would not know the rooms — the send path
/// reads them out of the store the sync fills.
///
/// Errors:
/// - [`SendError::NotSyncing`] if no sync is running. Refusing rather than
///   logging in again matters: a second login would mint a second device, and a
///   send on a client the UI is not listening to would report success for a
///   message that never syncs back.
pub async fn event(room_id: &str, event_type: &str, content: Value) -> Result<String, SendError> {
    let Some(client) = stream::client() else {
        return Err(SendError::NotSyncing);
    };
    event_with(&client, room_id, event_type, content).await
}

/// Posts one message-like event against a given client, and returns the event id
/// the server assigned.
///
/// Split out from [`event`] so the policy — which rooms may receive plaintext —
/// is a function of a room, testable without the global sync state. Every check
/// below this line is per-room; nothing here looks at `stream`.
///
/// `content` is the event content as it goes on the wire, so one function covers
/// everything the app sends: `m.text` and `m.emote` bodies, the `m.in_reply_to`
/// of a reply, the `m.annotation` of a reaction. Nothing here knows which is
/// which, because the spec does not need it to.
///
/// Errors:
/// - [`SendError::NotARoom`] / [`SendError::NoRoom`] if the id is malformed or
///   the room is not in the client's sync.
/// - [`SendError::Encrypted`] if the room is encrypted. **Nothing is sent.**
/// - [`SendError::Refused`] if the homeserver declines it.
pub async fn event_with(
    client: &Client,
    room_id: &str,
    event_type: &str,
    content: Value,
) -> Result<String, SendError> {
    let Ok(room_id) = RoomId::parse(room_id) else {
        return Err(SendError::NotARoom(room_id.to_owned()));
    };
    let Some(room) = client.get_room(&room_id) else {
        return Err(SendError::NoRoom(room_id.to_string()));
    };
    // Resolved, not merely read: `latest_encryption_state` asks the server when
    // a `/sync` did not carry the state, so a clear room is not refused because
    // of what happened to arrive in the filter. It still only ever resolves to
    // a decided answer, and an error here refuses rather than assumes.
    let state = room.latest_encryption_state().await.map_err(|e| {
        SendError::Refused(format!(
            "the encryption state of {room_id} is unreadable: {e}"
        ))
    })?;
    // Before the send, not after: matrix-sdk sends plaintext without complaint
    // when its crypto feature is off, so the only place to stop it is here.
    if leaks_plaintext(&state) {
        return Err(SendError::Encrypted(room_id.to_string()));
    }
    let sent = room
        .send_raw(event_type, content)
        .await
        .map_err(|e| SendError::Refused(e.to_string()))?;
    Ok(sent.response.event_id.to_string())
}

/// Whether sending to a room in this state would put plaintext where the room
/// expects ciphertext.
///
/// `NotEncrypted` is the only state that may send. Everything else refuses, and
/// that includes `Unknown` — which matrix-sdk returns when a `/sync` did not
/// carry enough state to decide — because an undecided room must not be treated
/// as a clear one. Written as a positive test of the one safe state so that a
/// state added later fails closed instead of open.
///
/// This is not a precaution, it is a known behaviour of the library: with the
/// `e2e-encryption` feature off, `Room::send_raw` posts the event as given,
/// under a `trace!` that says so, and returns success. In an encrypted room
/// that produces an `m.room.message` where every client expects
/// `m.room.encrypted`: a message nobody can read, visible to the server. So the
/// core refuses here and keeps refusing until the crypto store lands.
///
/// `m.reaction` is the one event the spec does *not* encrypt, and matrix-sdk
/// agrees (it skips encryption for it). It refuses too, because until the store
/// exists there is no way to tell that the exemption still applies to a given
/// room, and one readable message is a smaller loss than a broken timeline.
pub fn leaks_plaintext(state: &EncryptionState) -> bool {
    !matches!(state, EncryptionState::NotEncrypted)
}

#[cfg(test)]
mod tests {
    use matrix_sdk_test::{event_factory::EventFactory, JoinedRoomBuilder, SyncResponseBuilder};
    use ruma::OwnedRoomId;
    use serde_json::json;
    use wiremock::matchers::{method, path_regex};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;

    fn clear() -> EncryptionState {
        EncryptionState::NotEncrypted
    }

    fn unknown() -> EncryptionState {
        EncryptionState::Unknown
    }

    fn sealed() -> EncryptionState {
        EncryptionState::Encrypted
    }

    /// A client whose store holds one joined room, encrypted or not, fed by a
    /// canned `/sync` rather than a homeserver: `Room::new` is `pub(crate)` in
    /// matrix-sdk-base, so a room can only get into a store by being synced.
    ///
    /// Returns the server too, so a test can read its request log — which is how
    /// "nothing was sent" is proved rather than assumed.
    async fn client_with_room(encrypted: bool) -> (Client, MockServer, OwnedRoomId) {
        let server = MockServer::start().await;
        let room_id = OwnedRoomId::try_from("!room:hs").expect("a valid room id");

        let mut sync = SyncResponseBuilder::new();
        let room = JoinedRoomBuilder::new(&room_id);
        // The `m.room.encryption` event is what makes the room encrypted, and it
        // must land in the *state* block: that is where a homeserver puts it,
        // and a timeline placement would not tell matrix-sdk the room is sealed.
        let room = if encrypted {
            room.add_state_event(
                EventFactory::new()
                    .sender(*matrix_sdk_test::ALICE)
                    .room_encryption(),
            )
        } else {
            room
        };
        sync.add_joined_room(room);

        Mock::given(method("GET"))
            // `r0` or `v3`: ruma picks the prefix from the Matrix version the
            // client was pinned to, so the exact path is not ours to choose.
            .and(path_regex(r"^/_matrix/client/(r0|v3)/sync"))
            .respond_with(ResponseTemplate::new(200).set_body_json(sync.build_json_sync_response()))
            .mount(&server)
            .await;

        // A room whose `/sync` carried no encryption state is `Unknown`, and
        // `latest_encryption_state` then asks the server. This is the library's
        // own mock, because a 404 here has to carry a Matrix error body: matrix-sdk
        // reads the `errcode`, and a bare 404 makes it fail rather than decide.
        matrix_sdk_test::mocks::mock_encryption_state(&server, encrypted).await;

        let client = build_client_against(&server).await;
        // One `/sync` is enough to put the room in the store — and one is all we
        // want, since the mock answers forever. `LoopCtrl::Break` is how the
        // core's own loop is built too, so the fixture exercises the real path
        // rather than a back door into the store.
        client
            .sync_with_callback(matrix_sdk::config::SyncSettings::default(), |_| async {
                matrix_sdk::LoopCtrl::Break
            })
            .await
            .expect("the canned /sync is accepted");
        (client, server, room_id)
    }

    async fn build_client_against(server: &MockServer) -> Client {
        let client = matrix_sdk::test_utils::test_client_builder(Some(server.uri()))
            .request_config(matrix_sdk::config::RequestConfig::new().disable_retry())
            .build()
            .await
            .expect("the mock homeserver answers /versions");
        matrix_sdk::test_utils::set_client_session(&client).await;
        client
    }

    /// Every `PUT /rooms/../send/..` the server was asked for. An empty list is
    /// the proof: no send happened, whatever the call returned.
    async fn sends(server: &MockServer) -> Vec<String> {
        server
            .received_requests()
            .await
            // `None` means the mock server is gone. Treating that as "no
            // requests" would make every test below pass for the wrong reason.
            .expect("the mock server is still up")
            .iter()
            .map(|r| r.url.to_string())
            .filter(|url| url.contains("/send/"))
            .collect()
    }

    #[test]
    fn a_clear_room_may_send() {
        assert!(!leaks_plaintext(&clear()));
    }

    #[test]
    fn a_sealed_room_may_not_send() {
        assert!(leaks_plaintext(&sealed()));
    }

    #[test]
    fn an_undecided_room_may_not_send() {
        // The state that matters most: matrix-sdk returns it when the `/sync`
        // did not carry enough, and a room that might be encrypted must be
        // treated as if it is.
        assert!(leaks_plaintext(&unknown()));
    }

    #[tokio::test]
    async fn nothing_is_sent_to_an_encrypted_room() {
        let (client, server, room_id) = client_with_room(true).await;

        let got = event_with(
            &client,
            room_id.as_str(),
            "m.room.message",
            json!({"msgtype": "m.text", "body": "secret"}),
        )
        .await;

        match got {
            Err(SendError::Encrypted(ref room)) => assert_eq!(room, room_id.as_str()),
            other => panic!("an encrypted room must refuse, got {other:?}"),
        }
        let reached_the_wire = sends(&server).await;
        assert!(
            reached_the_wire.is_empty(),
            "the send must not reach the wire, but the core requested {reached_the_wire:?}"
        );
    }

    #[tokio::test]
    async fn nothing_is_sent_for_a_room_the_core_never_synced() {
        let (client, server, _room) = client_with_room(false).await;

        let got = event_with(
            &client,
            "!elsewhere:hs",
            "m.room.message",
            json!({"msgtype": "m.text", "body": "hello"}),
        )
        .await;

        assert!(matches!(got, Err(SendError::NoRoom(ref r)) if r == "!elsewhere:hs"));
        let reached_the_wire = sends(&server).await;
        assert!(
            reached_the_wire.is_empty(),
            "an unsynced room must refuse before any request, but {reached_the_wire:?} went out"
        );
    }

    #[tokio::test]
    async fn a_clear_room_does_send() {
        // The other half of the guard: a refusal that also refuses clear rooms
        // is not a security policy, it is a broken client. Without this the
        // tests above would pass with `event` returning `Encrypted` always.
        let (client, server, room_id) = client_with_room(false).await;
        Mock::given(method("PUT"))
            .and(path_regex(r"^/_matrix/client/(r0|v3)/rooms/.*/send/"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"event_id": "$sent:hs"})))
            .mount(&server)
            .await;

        let got = event_with(
            &client,
            room_id.as_str(),
            "m.room.message",
            json!({"msgtype": "m.text", "body": "hello"}),
        )
        .await;

        assert_eq!(got.unwrap(), "$sent:hs");
        let reached_the_wire = sends(&server).await;
        assert!(
            !reached_the_wire.is_empty(),
            "a clear room must reach the wire, or the refusals above prove nothing"
        );
    }

    #[tokio::test]
    async fn a_reaction_refuses_an_encrypted_room_like_a_message_does() {
        // The core never branches on `event_type`. Reactions are the one event
        // the spec leaves unencrypted, so a future reader may expect an
        // exemption here; there is none, because nothing in this build can
        // encrypt anything, and the guard is the room's state, not the event's
        // name.
        let (client, server, room_id) = client_with_room(true).await;

        let got = event_with(
            &client,
            room_id.as_str(),
            "m.reaction",
            json!({"m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$1:hs",
                "key": "\u{1f600}",
            }}),
        )
        .await;

        assert!(matches!(got, Err(SendError::Encrypted(_))));
        let reached_the_wire = sends(&server).await;
        assert!(
            reached_the_wire.is_empty(),
            "but {reached_the_wire:?} went out"
        );
    }
}
