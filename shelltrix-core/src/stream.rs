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
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use matrix_sdk::config::SyncSettings;
use matrix_sdk::{Client, LoopCtrl};
use tokio::sync::mpsc;

use crate::message_fields;

/// One event handed to Python, with the room it belongs to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamEvent {
    pub room_id: String,
    pub sender: String,
    pub origin_server_ts: u64,
    pub event_id: String,
    pub msgtype: String,
    pub body: String,
    pub mentions: bool,
}

/// The queue Python drains, independent of where the events come from.
///
/// Split out from the sync loop so the concurrency can be tested on its own:
/// the loop needs a homeserver, the queue does not.
struct Queue {
    /// `None` once the producer is gone. Dropping the sender is what closes
    /// the channel, and that is how a consumer learns the loop ended — so the
    /// sender is held in an `Option` rather than dropped with the struct.
    tx: Mutex<Option<mpsc::UnboundedSender<StreamEvent>>>,
    /// Behind its own async lock so that `pop` can wait WITHOUT holding the
    /// global lock: otherwise a wait would block `stop`, and `stop` blocking
    /// is how a shutdown hangs.
    rx: Arc<tokio::sync::Mutex<mpsc::UnboundedReceiver<StreamEvent>>>,
    /// Events already delivered, so a resumed sync does not replay the initial
    /// timeline the caller has seen.
    seen: Arc<Mutex<HashSet<String>>>,
    /// Why the producer stopped, if it did. Read when the channel closes, so
    /// Python learns "the connection dropped" instead of waiting forever.
    last_error: Arc<Mutex<Option<String>>>,
}

impl Queue {
    fn new() -> Arc<Self> {
        let (tx, rx) = mpsc::unbounded_channel();
        Arc::new(Self {
            tx: Mutex::new(Some(tx)),
            rx: Arc::new(tokio::sync::Mutex::new(rx)),
            seen: Arc::new(Mutex::new(HashSet::new())),
            last_error: Arc::new(Mutex::new(None)),
        })
    }

    /// Enqueues an event unless it was already delivered.
    ///
    /// De-duplicating here rather than in the sync loop means the guarantee
    /// holds for every producer, and it is testable without a network.
    fn push(&self, event: StreamEvent) {
        let Ok(mut seen) = self.seen.lock() else {
            return;
        };
        if !seen.insert(event.event_id.clone()) {
            return;
        }
        drop(seen);
        if let Ok(tx) = self.tx.lock() {
            if let Some(tx) = tx.as_ref() {
                let _ = tx.send(event);
            }
        }
    }

    async fn pop(&self, timeout_ms: u64) -> Result<Option<StreamEvent>, String> {
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

    let client = Client::builder()
        .homeserver_url(homeserver)
        .build()
        .await
        .map_err(|e| format!("cannot build a client for {homeserver}: {e}"))?;

    let login = client
        .matrix_auth()
        .login_username(user, password)
        .initial_device_display_name("shelltrix")
        .send()
        .await
        .map_err(|e| format!("login as {user} failed: {e}"))?;

    let queue = Queue::new();
    let task = tokio::spawn(run_sync(client.clone(), Arc::clone(&queue)));

    let mut joined_rooms: Vec<String> = client
        .rooms()
        .iter()
        .map(|r| r.room_id().to_string())
        .collect();
    joined_rooms.sort();

    let summary = StreamSummary {
        user_id: login.user_id.to_string(),
        device_id: login.device_id.to_string(),
        joined_rooms: {
            let mut rooms: Vec<String> = client
                .rooms()
                .iter()
                .map(|r| r.room_id().to_string())
                .collect();
            rooms.sort();
            rooms
        },
    };

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
pub async fn next(timeout_ms: u64) -> Result<Option<StreamEvent>, String> {
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

/// The sync loop. Never returns on its own: it runs until `stop`, the process
/// exits, or the homeserver becomes unreachable for good.
async fn run_sync(client: Client, queue: Arc<Queue>) {
    let for_loop = Arc::clone(&queue);
    let result = client
        .sync_with_callback(SyncSettings::default(), move |response| {
            let queue = Arc::clone(&for_loop);
            async move {
                push_events(&queue, &response);
                LoopCtrl::Continue
            }
        })
        .await;

    if let Err(error) = result {
        // Recording the reason is what makes the channel close meaningful.
        queue.close(format!("the sync loop stopped: {error}"));
    }
}

fn push_events(queue: &Queue, response: &matrix_sdk::sync::SyncResponse) {
    for (room_id, update) in &response.rooms.joined {
        for event in &update.timeline.events {
            // `kind.raw()` covers all three cases — decrypted, unable to
            // decrypt, plaintext — so an encrypted room needs no second path.
            // `json().get()` is the raw JSON text; parsing it here keeps the
            // field extraction in one place, shared with `parse_sync_messages`.
            let Ok(value) =
                serde_json::from_str::<serde_json::Value>(event.kind.raw().json().get())
            else {
                continue;
            };
            let Some(object) = value.as_object() else {
                continue;
            };
            let Some(fields) = message_fields(object) else {
                continue;
            };
            queue.push(StreamEvent {
                room_id: room_id.to_string(),
                sender: fields.sender,
                origin_server_ts: fields.origin_server_ts,
                event_id: fields.event_id,
                msgtype: fields.msgtype,
                body: fields.body,
                mentions: fields.mentions,
            });
        }
    }
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

    #[test]
    fn stop_is_safe_to_call_repeatedly() {
        stop();
        stop();
        assert!(stream().lock().unwrap().is_none());
    }

    /// The reason this is worth a test: `next` parks while holding locks. If
    /// it held the *global* one, `stop` could never take effect until the wait
    /// expired, which is how quitting the app would hang.
    fn event(event_id: &str) -> StreamEvent {
        StreamEvent {
            room_id: "!r:hs".to_owned(),
            sender: "@alice:hs".to_owned(),
            origin_server_ts: 1_700_000_000_000,
            event_id: event_id.to_owned(),
            msgtype: "m.text".to_owned(),
            body: format!("body of {event_id}"),
            mentions: false,
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
