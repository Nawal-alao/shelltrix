//! The Matrix transport, on matrix-sdk.
//!
//! Everything here returns plain data. The Python facade turns it into the
//! dataclasses of `shelltrix.events`, so no matrix-sdk type ever crosses the
//! boundary — which is what keeps the UI independent of this crate.
//!
//! What this module does NOT do yet: the sync loop. `Client::sync()` never
//! returns, so the real transport needs a background task pushing events into
//! a queue Python drains. That is step 3.2 proper; what exists here is the
//! path that must work first — proving that Python can drive matrix-sdk at
//! all, over the runtime bridge, against a real homeserver.

use matrix_sdk::config::SyncSettings;
use matrix_sdk::{Client, LoopCtrl};

/// What one `/sync` told us, as plain data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncSummary {
    pub user_id: String,
    pub device_id: String,
    /// Room identifiers, sorted, so the result is stable across runs.
    pub joined_rooms: Vec<String>,
}

/// Logs in and runs a single `/sync`.
///
/// Errors are returned as `String` so the PyO3 layer can raise them without
/// depending on matrix-sdk's error type here.
pub async fn login_and_sync(
    homeserver: &str,
    user: &str,
    password: &str,
) -> Result<SyncSummary, String> {
    // `homeserver_url` alone is deliberate: it skips the `/.well-known`
    // lookup, which shelltrix does itself at login time and which would fail
    // for the self-signed homeservers people self-host.
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

    // One pass, not the client's main loop: `sync()` returns
    // `LoopCtrl::Continue` forever. The background loop is a later step; this
    // must be able to come back to Python.
    client
        .sync_with_callback(SyncSettings::default(), |_| async { LoopCtrl::Break })
        .await
        .map_err(|e| format!("sync failed: {e}"))?;

    let mut joined_rooms: Vec<String> = client
        .rooms()
        .iter()
        .map(|room| room.room_id().to_string())
        .collect();
    joined_rooms.sort();

    Ok(SyncSummary {
        user_id: login.user_id.to_string(),
        device_id: login.device_id.to_string(),
        joined_rooms,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// No network in `cargo test`, so this only checks that we do not pretend
    /// to have logged in when the homeserver is unreachable: a wrong refactor
    /// here would make shelltrix believe it is connected while offline.
    #[tokio::test]
    async fn reports_failure_instead_of_an_empty_success() {
        // Port 1 is reserved and refuses connections immediately.
        let got = login_and_sync("http://127.0.0.1:1", "@a:hs", "pw").await;
        assert!(
            got.is_err(),
            "an unreachable homeserver must not look like success"
        );
        assert!(got.unwrap_err().contains("127.0.0.1:1"));
    }
}
