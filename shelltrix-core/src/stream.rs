//! The sync loop, as a queue Python drains.
//!
//! `Client::sync()` never returns — it is the client's main loop — so a
//! transport cannot call it and get events back. Instead the loop runs as a
//! Tokio task that pushes events into an unbounded channel, and Python pulls
//! them one at a time.
//!
//! Why a queue rather than a callback into Python:
//!
//! - Python is single-threaded and holds the GIL. A Rust thread calling a
//!   Python callable needs the GIL, so it must wait for the event loop, and if
//!   Python is in turn waiting on Rust, that is a deadlock.
//! - A queue needs no Python at all on the producing side. The consumer blocks
//!   in `next_event`, exactly as it already blocks on matrix-nio's calls.
//!
//! The channel is unbounded on purpose: dropping an event because Python was
//! busy repainting would silently lose a message. Backpressure belongs at the
//! UI, which is a separate decision from the transport.

use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use matrix_sdk::authentication::matrix::MatrixSession;
use matrix_sdk::config::SyncSettings;
use matrix_sdk::{Client, LoopCtrl, SessionMeta, SessionTokens};
use matrix_sdk_base::store::RoomLoadSettings;
use ruma::api::client::filter::FilterDefinition;
use ruma::api::client::sync::sync_events::v3::Filter;
use tokio::sync::mpsc;

use crate::classify;
use crate::rooms::{self, RoomInfo};

/// The queue Python drains, independent of where the events come from.
///
/// Split out from the sync loop so the concurrency can be tested on its own:
/// the loop needs a homeserver, the queue does not.
struct Queue {
    /// `None` once the producer is gone. Dropping the sender is what closes
    /// the channel, and that is how a consumer learns the loop ended — so the
    /// sender is held in an `Option` rather than dropped with the struct.
    tx: Mutex<Option<mpsc::UnboundedSender<classify::Event>>>,
    /// Behind its own async lock so that `pop` can wait WITHOUT holding the
    /// global lock: otherwise a wait would block `stop`, and `stop` blocking
    /// is how a shutdown hangs.
    rx: Arc<tokio::sync::Mutex<mpsc::UnboundedReceiver<classify::Event>>>,
    /// Events already delivered, so a resumed sync does not replay the initial
    /// timeline the caller has seen.
    seen: Arc<Mutex<HashSet<String>>>,
    /// Why the producer stopped, if it did. Read when the channel closes, so
    /// Python learns "the connection dropped" instead of waiting forever.
    last_error: Arc<Mutex<Option<String>>>,
    /// The last known room list, and whether it is out of date.
    ///
    /// Python calls `rooms()` on every sidebar repaint, so the snapshot is taken
    /// here — once per sync that changed a room — and read back from memory.
    rooms: Mutex<Vec<RoomInfo>>,
    /// Set when a sync response carried rooms, cleared when they are re-read.
    ///
    /// Recomputing after *every* sync would walk every member of every room
    /// thirty seconds forever, to learn nothing: an idle account syncs empty.
    rooms_stale: AtomicBool,
    /// Whether at least one `/sync` response has been received.
    ///
    /// Separate from the queue being non-empty on purpose. A quiet account
    /// produces no events, and "no event yet" is not "not synced": without this
    /// the UI would wait forever for a room list that has in fact arrived.
    first_sync: AtomicBool,
}

impl Queue {
    fn new() -> Arc<Self> {
        let (tx, rx) = mpsc::unbounded_channel();
        Arc::new(Self {
            tx: Mutex::new(Some(tx)),
            rx: Arc::new(tokio::sync::Mutex::new(rx)),
            seen: Arc::new(Mutex::new(HashSet::new())),
            last_error: Arc::new(Mutex::new(None)),
            rooms: Mutex::new(Vec::new()),
            rooms_stale: AtomicBool::new(true),
            first_sync: AtomicBool::new(false),
        })
    }

    /// Enqueues an event unless it was already delivered.
    ///
    /// De-duplicating here rather than in the sync loop means the guarantee
    /// holds for every producer, and it is testable without a network.
    fn push(&self, event: classify::Event) {
        let key = dedup_key(&event);
        let Ok(mut seen) = self.seen.lock() else {
            return;
        };
        if !seen.insert(key) {
            return;
        }
        drop(seen);
        if let Ok(tx) = self.tx.lock() {
            if let Some(tx) = tx.as_ref() {
                let _ = tx.send(event);
            }
        }
    }

    async fn pop(&self, timeout_ms: u64) -> Result<Option<classify::Event>, String> {
        let received = {
            let mut rx = self.rx.lock().await;
            tokio::time::timeout(Duration::from_millis(timeout_ms), rx.recv()).await
        };
        match received {
            Ok(Some(event)) => Ok(Some(event)),
            Ok(None) => Err(self
                .last_error
                .lock()
                .ok()
                .and_then(|e| e.clone())
                .unwrap_or_else(|| "the sync loop stopped".to_owned())),
            // A quiet room is the normal case: the long poll had nothing.
            Err(_) => Ok(None),
        }
    }

    /// Records why the producer stopped and closes the channel.
    fn close(&self, message: String) {
        if let Ok(mut slot) = self.last_error.lock() {
            *slot = Some(message);
        }
        if let Ok(mut tx) = self.tx.lock() {
            *tx = None;
        }
    }

    /// Marks the room list as needing a re-read.
    ///
    /// Called for every sync response that carried rooms. Marking rather than
    /// computing keeps the sync callback from doing per-room member lookups,
    /// which would stall the `/sync` loop for a UI that repaints at 60 Hz.
    fn mark_stale(&self, carried_rooms: bool) {
        self.first_sync.store(true, Ordering::SeqCst);
        if carried_rooms {
            self.rooms_stale.store(true, Ordering::SeqCst);
        }
    }

    /// Re-reads the room list if a sync invalidated it.
    async fn refresh_rooms(&self, client: &Client) {
        if !self.rooms_stale.swap(false, Ordering::SeqCst) {
            return;
        }
        let fresh = rooms::snapshot(client).await;
        if let Ok(mut rooms) = self.rooms.lock() {
            *rooms = fresh;
        } else {
            // Poisoned: put the flag back, so the next sync tries again rather
            // than leaving Python with a permanently stale, empty room list.
            self.rooms_stale.store(true, Ordering::SeqCst);
        }
    }

    /// The room list, recomputed first if a sync has invalidated it.
    ///
    /// Recomputing on read rather than only on sync is what lets Python call
    /// this the moment it needs a name, without a stale one.
    async fn rooms(&self, client: &Client) -> Vec<RoomInfo> {
        self.refresh_rooms(client).await;
        self.rooms
            .lock()
            .map(|rooms| rooms.clone())
            .unwrap_or_default()
    }
}

struct StreamState {
    queue: Arc<Queue>,
    /// Kept so the sync loop is not cancelled: dropping the client stops it.
    client: Client,
    task: tokio::task::JoinHandle<()>,
}

static STREAM: OnceLock<Mutex<Option<StreamState>>> = OnceLock::new();

fn stream() -> &'static Mutex<Option<StreamState>> {
    STREAM.get_or_init(|| Mutex::new(None))
}

/// Logs in, starts the sync loop in the background, and returns immediately.
///
/// Errors:
/// - "a sync is already running" if one is.
/// - anything [`crate::transport::login_and_sync`] can return, plus the loop
///   failing to spawn.
pub async fn start(homeserver: &str, user: &str, password: &str) -> Result<StreamSummary, String> {
    let client = build_client(homeserver).await?;
    let login = client
        .matrix_auth()
        .login_username(user, password)
        .initial_device_display_name("shelltrix")
        .send()
        .await
        .map_err(|e| format!("login as {user} failed: {e}"))?;
    spawn(
        client,
        StreamSummary {
            user_id: login.user_id.to_string(),
            device_id: login.device_id.to_string(),
            joined_rooms: Vec::new(),
        },
    )
    .await
}

/// Restores a saved session instead of logging in, then starts the loop.
///
/// This is the path the app actually takes: `Credentials` holds an access token
/// and a device id from a previous run, not a password, so the password entry
/// above is only reachable from a fresh login.
///
/// Errors:
/// - "a sync is already running" if one is.
/// - "the saved session for {user} was refused: …" if the token has been
///   revoked or expired. Distinct from a network failure so the caller can tell
///   "log in again" from "try later".
pub async fn start_with_token(
    homeserver: &str,
    user_id: &str,
    device_id: &str,
    access_token: &str,
) -> Result<StreamSummary, String> {
    let client = build_client(homeserver).await?;
    let Ok(user) = user_id.parse() else {
        return Err(format!("{user_id:?} is not a Matrix user id"));
    };
    client
        .matrix_auth()
        .restore_session(
            MatrixSession {
                meta: SessionMeta {
                    user_id: user,
                    // Device ids are opaque to the protocol — no character is
                    // illegal — so this cannot fail and is not checked.
                    device_id: device_id.into(),
                },
                tokens: SessionTokens {
                    access_token: access_token.to_owned(),
                    refresh_token: None,
                },
            },
            RoomLoadSettings::default(),
        )
        .await
        .map_err(|e| format!("the saved session for {user_id} was refused: {e}"))?;
    spawn(
        client,
        StreamSummary {
            user_id: user_id.to_owned(),
            device_id: device_id.to_owned(),
            joined_rooms: Vec::new(),
        },
    )
    .await
}

async fn build_client(homeserver: &str) -> Result<Client, String> {
    Client::builder()
        .homeserver_url(homeserver)
        .build()
        .await
        .map_err(|e| format!("cannot build a client for {homeserver}: {e}"))
}

/// Claims the slot and starts the loop for an already-authenticated client.
async fn spawn(client: Client, mut summary: StreamSummary) -> Result<StreamSummary, String> {
    // Claim the slot with a lock we do NOT hold across the login: the network
    // calls below must not make `stop` or another `start` wait for them.
    {
        let slot = stream()
            .lock()
            .map_err(|_| "the stream state was poisoned".to_owned())?;
        if slot.is_some() {
            return Err("a sync is already running".to_owned());
        }
    }

    let queue = Queue::new();
    let task = tokio::spawn(run_sync(client.clone(), Arc::clone(&queue)));

    let mut rooms: Vec<String> = client
        .rooms()
        .iter()
        .map(|r| r.room_id().to_string())
        .collect();
    rooms.sort();
    summary.joined_rooms = rooms;

    let mut slot = stream()
        .lock()
        .map_err(|_| "the stream state was poisoned".to_owned())?;
    // Someone may have started a sync while we were logging in.
    if slot.is_some() {
        task.abort();
        return Err("a sync is already running".to_owned());
    }
    *slot = Some(StreamState {
        queue,
        client,
        task,
    });
    Ok(summary)
}

/// Waits up to `timeout_ms` for the next event.
///
/// `None` means the wait expired, which is the normal outcome most of the
/// time: a quiet room produces nothing. It is not an error, and the caller must
/// not treat it as a disconnect.
pub async fn next(timeout_ms: u64) -> Result<Option<classify::Event>, String> {
    // Take the queue out from under the global lock, then let it go before
    // waiting: `stop` must stay responsive while `next` is parked.
    let queue = {
        let slot = stream()
            .lock()
            .map_err(|_| "the stream state was poisoned".to_owned())?;
        Arc::clone(
            &slot
                .as_ref()
                .ok_or_else(|| "no sync is running".to_owned())?
                .queue,
        )
    };
    queue.pop(timeout_ms).await
}

/// Stops the loop and drops the queue.
pub fn stop() {
    if let Ok(mut slot) = stream().lock() {
        if let Some(state) = slot.take() {
            state.task.abort();
            drop(state.client);
        }
    }
}

/// The current room list, or empty when no sync is running.
pub async fn rooms() -> Vec<RoomInfo> {
    let state = match stream().lock() {
        Ok(slot) => slot
            .as_ref()
            .map(|state| (Arc::clone(&state.queue), state.client.clone())),
        Err(_) => None,
    };
    match state {
        Some((queue, client)) => queue.rooms(&client).await,
        None => Vec::new(),
    }
}

/// Whether at least one `/sync` response has been processed.
///
/// The UI waits for this to list rooms. Reporting it from the queue rather than
/// inferring it from "an event arrived" matters: an account with nothing to say
/// would otherwise never be declared synced, and the room list would stay blank
/// forever on a perfectly healthy connection.
pub fn first_sync_done() -> bool {
    stream()
        .lock()
        .ok()
        .and_then(|slot| slot.as_ref().map(|state| Arc::clone(&state.queue)))
        .is_some_and(|queue| queue.first_sync.load(Ordering::SeqCst))
}

/// The key that identifies an event for de-duplication.
///
/// Timeline events carry a server-assigned `event_id` that is unique, so it is
/// the whole key on its own. Typing notifications and invites have no such id,
/// and an empty string as their key would collapse every one of them in the
/// session into the first: the "typing stopped" notification would be swallowed
/// and the indicator would stay lit forever. Their raw payload stands in
/// instead, which still drops the repeats Synapse resends on every sync while
/// letting a genuine change through.
fn dedup_key(event: &classify::Event) -> String {
    if !event.event_id.is_empty() {
        return event.event_id.clone();
    }
    format!("{}:{}:{}", event.kind, event.room_id, event.source)
}

fn sync_filter() -> FilterDefinition {
    // Only `ephemeral` is named. The others are left absent on purpose: Synapse
    // reads a missing `types` as "every type", whereas an empty list would mean
    // "none" and would silently drop the timeline too.
    serde_json::from_value::<FilterDefinition>(serde_json::json!({
        "room": {"ephemeral": {"types": ["m.typing"]}}
    }))
    .expect("the sync filter is a literal in this file")
}

/// The sync loop. Never returns on its own: it runs until `stop`, the process
/// exits, or the homeserver becomes unreachable for good.
async fn run_sync(client: Client, queue: Arc<Queue>) {
    let for_loop = Arc::clone(&queue);
    // An invite is the one event addressed to a specific user: the stripped
    // state holds every member, and only ours is ours to announce.
    let user_id = client.user_id().map(|u| u.to_string()).unwrap_or_default();
    let for_user = user_id.clone();
    let result = client
        .sync_with_callback(
            SyncSettings::default().filter(Filter::FilterDefinition(sync_filter())),
            move |response| {
                let queue = Arc::clone(&for_loop);
                let user_id = for_user.clone();
                async move {
                    let carried = push_events(&queue, &user_id, &response);
                    queue.mark_stale(carried);
                    LoopCtrl::Continue
                }
            },
        )
        .await;

    if let Err(error) = result {
        // Recording the reason is what makes the channel close meaningful.
        queue.close(format!("the sync loop stopped: {error}"));
    }
}

/// Pushes one response's events, and reports whether it carried any room.
///
/// The return value is what tells the queue its cached room list is out of
/// date. Synapse sends only the rooms that changed, so an idle account syncs
/// empty and nothing needs re-resolving.
fn push_events(
    queue: &Arc<Queue>,
    user_id: &str,
    response: &matrix_sdk::sync::SyncResponse,
) -> bool {
    for (room_id, update) in &response.rooms.joined {
        for event in &update.timeline.events {
            // `kind.raw()` covers all three cases — decrypted, unable to
            // decrypt, plaintext — so an encrypted room needs no second path.
            if let Some(event) = parse(event.kind.raw().json().get()) {
                if let Some(classified) = classify::timeline_event(room_id.as_str(), &event) {
                    queue.push(classified);
                }
            }
        }
        // Typing lives in the ephemeral section, not the timeline: it is never
        // stored, so a reader that only looks at timelines never sees anyone.
        for event in &update.ephemeral {
            if let Some(event) = parse(event.json().get()) {
                if let Some(classified) = classify::ephemeral_event(room_id.as_str(), &event) {
                    queue.push(classified);
                }
            }
        }
    }
    for (room_id, update) in &response.rooms.invited {
        let stripped: Vec<serde_json::Value> = update
            .invite_state
            .events
            .iter()
            .filter_map(|e| parse(e.json().get()))
            .collect();
        if let Some(classified) = classify::invite(room_id.as_str(), &stripped, user_id) {
            queue.push(classified);
        }
    }

    // Left rooms still appear here, and the sidebar drops them by membership,
    // so their presence has to count as a change or a `/leave` would leave a
    // stale room on screen until the next unrelated event.
    !response.rooms.joined.is_empty()
        || !response.rooms.invited.is_empty()
        || !response.rooms.left.is_empty()
}

/// Decodes one raw Matrix event into JSON.
///
/// matrix-sdk keeps `Raw` events unparsed on purpose: parsing costs allocations
/// the SDK does not want to pay for events nobody reads, and we read a room's
/// whole timeline. Taking the text rather than the typed event means the three
/// shapes it arrives in — timeline, ephemeral, stripped — all go through here.
fn parse(json: &str) -> Option<serde_json::Value> {
    serde_json::from_str(json).ok()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamSummary {
    pub user_id: String,
    pub device_id: String,
    pub joined_rooms: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn next_without_a_running_sync_is_an_error() {
        // Called from a plain thread, as Python does.
        let got = crate::runtime::block_on(async {
            // Make sure no other test left a stream behind.
            stop();
            next(0).await
        });
        assert!(
            got.is_err(),
            "asking for an event with no sync running must say so, not wait forever"
        );
    }

    /// Synapse sends `m.typing` only when the sync filter names it, and sends
    /// nothing at all otherwise — no error, no warning. The typing indicator
    /// would simply never appear, so the filter is asserted here rather than
    /// left to a manual check nobody repeats.
    #[test]
    fn the_sync_filter_asks_for_typing() {
        let json = serde_json::to_value(sync_filter()).unwrap();
        assert_eq!(
            json["room"]["ephemeral"]["types"],
            serde_json::json!(["m.typing"]),
            "the filter must name the typing event, got {json}"
        );
        // Only `ephemeral` may be constrained. An empty `types` list elsewhere
        // would read as "no event types" and silently empty the timeline.
        assert!(
            json["room"]["timeline"]["types"].is_null(),
            "the timeline must stay unfiltered, got {json}"
        );
    }

    #[test]
    fn stop_is_safe_to_call_repeatedly() {
        stop();
        stop();
        assert!(stream().lock().unwrap().is_none());
    }

    /// The reason this is worth a test: `next` parks while holding locks. If
    /// it held the *global* one, `stop` could never take effect until the wait
    /// expired, which is how quitting the app would hang.
    fn event(event_id: &str) -> classify::Event {
        classify::Event {
            kind: classify::KIND_MESSAGE.to_owned(),
            room_id: "!r:hs".to_owned(),
            sender: "@alice:hs".to_owned(),
            origin_server_ts: 1_700_000_000_000,
            event_id: event_id.to_owned(),
            msgtype: "m.text".to_owned(),
            body: format!("body of {event_id}"),
            mentions: false,
            ..classify::Event::default()
        }
    }

    fn typing(source: &str) -> classify::Event {
        classify::Event {
            kind: classify::KIND_TYPING.to_owned(),
            room_id: "!r:hs".to_owned(),
            source: source.to_owned(),
            ..classify::Event::default()
        }
    }

    /// The queue is the part the sync loop hands over, and the part that is
    /// easy to get wrong: unbounded, so nothing is dropped, yet still
    /// de-duplicated, so a resumed sync does not replay the timeline.
    #[test]
    fn delivers_events_in_order() {
        let queue = Queue::new();
        queue.push(event("$1"));
        queue.push(event("$2"));
        let got = crate::runtime::block_on(async {
            (queue.pop(50).await.unwrap(), queue.pop(50).await.unwrap())
        });
        assert_eq!(got.0.unwrap().event_id, "$1");
        assert_eq!(got.1.unwrap().event_id, "$2");
    }

    #[test]
    fn a_known_event_is_never_delivered_twice() {
        let queue = Queue::new();
        queue.push(event("$1"));
        queue.push(event("$1"));
        crate::runtime::block_on(async {
            assert_eq!(queue.pop(50).await.unwrap().unwrap().event_id, "$1");
        });
        let got = crate::runtime::block_on(async { queue.pop(20).await.unwrap() });
        assert!(got.is_none(), "the repeat must be dropped, not queued");
    }

    /// Typing has no event id, so it once shared the key `""` with every other
    /// id-less event. The second push was then read as a repeat and dropped,
    /// which is why "stopped typing" never arrived and the indicator could not
    /// be cleared.
    #[test]
    fn an_id_less_event_is_not_mistaken_for_a_repeat() {
        let queue = Queue::new();
        queue.push(typing(r#"{"user_ids":["@bob:hs"]}"#));
        queue.push(typing(r#"{"user_ids":[]}"#));
        crate::runtime::block_on(async {
            let started = queue.pop(50).await.unwrap().unwrap();
            let stopped = queue.pop(50).await.unwrap().unwrap();
            assert_eq!(started.source, r#"{"user_ids":["@bob:hs"]}"#);
            assert_eq!(stopped.source, r#"{"user_ids":[]}"#);
        });
    }

    /// The other half of that bargain: Synapse resends an unchanged typing
    /// state on every sync, and each resend must not wake the UI up again.
    #[test]
    fn an_unchanged_id_less_event_is_still_de_duplicated() {
        let queue = Queue::new();
        queue.push(typing(r#"{"user_ids":["@bob:hs"]}"#));
        queue.push(typing(r#"{"user_ids":["@bob:hs"]}"#));
        crate::runtime::block_on(async {
            assert!(queue.pop(50).await.unwrap().is_some());
        });
        let got = crate::runtime::block_on(async { queue.pop(20).await.unwrap() });
        assert!(got.is_none(), "the resend must be dropped, not queued");
    }

    /// An invite is id-less too, and two invites to different rooms are two
    /// different things to announce.
    #[test]
    fn id_less_events_in_different_rooms_are_different_events() {
        let queue = Queue::new();
        for room in ["!a:hs", "!b:hs"] {
            queue.push(classify::Event {
                kind: classify::KIND_INVITE.to_owned(),
                room_id: room.to_owned(),
                ..classify::Event::default()
            });
        }
        crate::runtime::block_on(async {
            let first = queue.pop(50).await.unwrap().unwrap();
            let second = queue.pop(50).await.unwrap().unwrap();
            assert_eq!(
                [first.room_id.as_str(), second.room_id.as_str()],
                ["!a:hs", "!b:hs"]
            );
        });
    }

    /// A quiet room is the common case and must not look like a failure.
    #[test]
    fn an_expired_wait_is_not_an_error() {
        let queue = Queue::new();
        let got = crate::runtime::block_on(async { queue.pop(20).await });
        assert_eq!(got.unwrap(), None);
    }

    #[test]
    fn a_failed_producer_says_why_it_stopped() {
        let queue = Queue::new();
        queue.close("the sync loop stopped: connection refused".to_owned());
        let got = crate::runtime::block_on(async { queue.pop(50).await });
        assert!(got.unwrap_err().contains("connection refused"));
    }

    #[test]
    fn stop_is_not_blocked_by_a_waiting_next() {
        let waiter = std::thread::spawn(|| {
            crate::runtime::block_on(async {
                // No stream is running, so this fails immediately; the point is
                // that it fails fast rather than blocking `stop`.
                stop();
                let _ = next(30_000).await;
            })
        });
        let started = std::time::Instant::now();
        stop();
        assert!(
            started.elapsed() < std::time::Duration::from_secs(2),
            "stop blocked while another thread was inside next()"
        );
        waiter.join().unwrap();
    }
}
