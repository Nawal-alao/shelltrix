//! The room list, resolved from the sync state into what the UI renders.
//!
//! The sync stream carries events, and an event only says which room it
//! happened in. The UI also needs a *name* for that room and the names of the
//! people in it: without them a sidebar full of `!AbCdEf:matrix.org` is
//! technically correct and unusable.
//!
//! So this is the second thing a transport owes Python, next to events. It is
//! a snapshot rather than a stream because room names change far more slowly
//! than messages arrive, and because `matrix_client.rooms()` is synchronous —
//! the UI calls it on every repaint of the sidebar.

use std::collections::HashMap;

use matrix_sdk::{Client, Room, RoomMemberships};
use pyo3::prelude::*;

/// One room, named.
#[pyclass(frozen, get_all, skip_from_py_object, module = "shelltrix_core")]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RoomInfo {
    /// `!AbCdEf:matrix.org`, or `#alias:matrix.org` when that is all we have.
    pub room_id: String,
    /// What the sidebar prints: the room name, else the alias, else the
    /// members' names, else the room id. Never empty.
    pub display_name: String,
    /// The `m.room.name` as set, which may be empty even when `display_name`
    /// is not. The UI shows the difference between "named" and "computed".
    pub name: String,
    /// Disambiguated member names, `user_id` to what to print for them.
    ///
    /// A member with no display name is absent rather than mapped to their
    /// user id: the UI then falls back to the id itself, which is what
    /// matrix-nio's `user_name()` did.
    pub user_names: HashMap<String, String>,
}

#[pymethods]
impl RoomInfo {
    fn __repr__(&self) -> String {
        format!(
            "RoomInfo(room_id={:?}, name={:?})",
            self.room_id, self.display_name
        )
    }
}

/// Every room the client currently knows about, sorted by id.
///
/// Joined *and* invited: an invite the user has not answered still has a room,
/// and the sidebar lists it as pending.
pub async fn snapshot(client: &Client) -> Vec<RoomInfo> {
    let mut out = Vec::new();
    for room in client.rooms() {
        out.push(info(room).await);
    }
    // Sorted so the Python cache rebuild is deterministic, which is what makes
    // the parity tests possible.
    out.sort_by(|a, b| a.room_id.cmp(&b.room_id));
    out
}

/// The name to print for a room, following the chain the spec prescribes.
///
/// matrix-sdk 0.19 computes this, but only from a `RoomSummary` it does not
/// re-export, so there is no way to ask it. Rather than call the homeserver
/// `/summary` per room, the chain is reproduced here from what `Room` does
/// expose: the name it was given, its canonical alias, and its heroes.
///
/// The order is the spec's, and skipping a step is what produces the classic
/// "Empty room" or "Alice, Bob and 4 others" in a room called "Planning":
/// 1. the room name, else
/// 2. the canonical alias, else
/// 3. the members' names, counted against how many there are.
fn resolve(
    name: Option<&str>,
    canonical_alias: Option<&str>,
    hero_names: &[&str],
    num_joined: usize,
) -> String {
    let name = name.map(str::trim).filter(|name| !name.is_empty());
    if let Some(name) = name {
        return name.to_owned();
    }
    if let Some(alias) = canonical_alias
        .map(str::trim)
        .filter(|alias| !alias.is_empty())
    {
        return alias.to_owned();
    }
    from_heroes(hero_names, num_joined)
}

/// The last step of the chain: what to call a room made of people.
///
/// Sorted, because the same two members must not produce "Alice and Bob" in
/// one session and "Bob and Alice" in the next — the sidebar would show two
/// different entries for one room.
fn from_heroes(hero_names: &[&str], num_joined: usize) -> String {
    let mut heroes: Vec<&str> = hero_names.to_vec();
    heroes.sort_unstable();
    heroes.dedup();

    let num_others = num_joined.saturating_sub(1);
    let joined = if num_joined > 1 {
        if heroes.is_empty() && num_joined > 1 {
            // Nobody has a display name, so the count is all there is to say.
            format!("{num_joined} people")
        } else if heroes.len() >= num_others {
            heroes.join(", ")
        } else {
            format!(
                "{}, and {} others",
                heroes.join(", "),
                num_joined - heroes.len()
            )
        }
    } else {
        String::new()
    };

    joined
}

/// Resolves one room.
async fn info(room: Room) -> RoomInfo {
    let room_id = room.room_id().to_string();
    let members = members(&room).await;
    let heroes = hero_names(&room).await;
    let alias = room.canonical_alias();
    let display_name = resolve(
        room.name().as_deref(),
        alias.as_ref().map(|alias| alias.as_str()),
        &heroes.iter().map(String::as_str).collect::<Vec<_>>(),
        members.len(),
    );
    RoomInfo {
        display_name: with_fallback(&room_id, display_name),
        name: room.name().unwrap_or_default(),
        user_names: members,
        room_id,
    }
}

/// The name to print, or the room id if there is none.
///
/// A nameless, alias-less, member-less room exists — a DM whose contact left.
/// Python reads `display_name` with no fallback of its own, so the guarantee
/// has to hold here or the sidebar renders a blank row that reads as a bug in
/// the app rather than a nameless room.
fn with_fallback(room_id: &str, display_name: String) -> String {
    if display_name.is_empty() {
        room_id.to_owned()
    } else {
        display_name
    }
}

/// The display name of every joined member, and how many there are.
///
/// `members_no_sync` and not `members`: the latter may issue a `/members`
/// request per room, and the sync loop has already delivered the member list of
/// every room it changed. A room whose members were never sent simply yields
/// no names, which the UI handles by printing the user id.
async fn members(room: &Room) -> HashMap<String, String> {
    let mut out = HashMap::new();
    let Ok(found) = room.members_no_sync(RoomMemberships::JOIN).await else {
        return out;
    };
    for member in found {
        if let Some(name) = member.display_name() {
            if !name.is_empty() {
                out.insert(member.user_id().to_string(), name.to_owned());
            }
        }
    }
    out
}

/// The names to build an unnamed room's label from.
///
/// The SDK already works out who the "heroes" are — the members that actually
/// appear in the room name — so this only has to collect what it decided,
/// rather than re-derive the notion and get it subtly different.
async fn hero_names(room: &Room) -> Vec<String> {
    room.heroes()
        .await
        .into_iter()
        .filter_map(|hero| hero.display_name)
        .filter(|name| !name.is_empty())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The reason `with_fallback` exists. A room can be nameless, alias-less
    /// and emptied of members — a DM whose contact left — and there is then
    /// nothing to call it. Python trusts `display_name`, so this is the only
    /// thing standing between that and a blank sidebar row.
    #[test]
    fn a_room_with_no_name_falls_back_to_its_id() {
        assert_eq!(with_fallback("!a:hs", String::new()), "!a:hs");
    }

    /// And a room that does have a name keeps it verbatim: the id is a
    /// fallback, not a prefix, and prefixing it would break every room name.
    #[test]
    fn a_named_room_keeps_its_name() {
        assert_eq!(with_fallback("!a:hs", "Planning".to_owned()), "Planning");
    }

    /// Step 1 of the chain, and it outranks everything: a named room is called
    /// its name even when it has six members whose names are available.
    #[test]
    fn a_named_room_is_its_name() {
        assert_eq!(
            resolve(Some("Planning"), Some("#plan:hs"), &["Alice", "Bob"], 6),
            "Planning"
        );
    }

    /// An empty name is not a name. Synapse accepts `""`, and treating it as
    /// one would show a room called nothing at all.
    #[test]
    fn an_empty_name_falls_through() {
        assert_eq!(resolve(Some("   "), Some("#plan:hs"), &[], 3), "#plan:hs");
    }

    /// Step 2: no name, so the canonical alias. This is what keeps a public
    /// room findable — "#matrix:hs" rather than a list of its members.
    #[test]
    fn an_alias_is_used_before_the_members() {
        assert_eq!(resolve(None, Some("#plan:hs"), &["Alice"], 3), "#plan:hs");
    }

    /// Step 3, one member short of ours: a DM is the other person's name.
    #[test]
    fn a_two_person_room_is_titled_after_the_other() {
        assert_eq!(resolve(None, None, &["Alice"], 2), "Alice");
    }

    /// Two others listed in full, with no "and N others" — at three people the
    /// exhaustive form is the readable one.
    #[test]
    fn a_small_room_lists_everyone() {
        assert_eq!(resolve(None, None, &["Alice", "Bob"], 3), "Alice, Bob");
    }

    /// The rest of a large room, and the reason this is reproduced rather than
    /// approximated: a sidebar row must fit, so the names are counted off.
    #[test]
    fn a_large_room_counts_the_others() {
        assert_eq!(
            resolve(None, None, &["Alice", "Bob"], 6),
            "Alice, Bob, and 4 others"
        );
    }

    /// Heroes with no display name leave nothing to build a name from, so the
    /// count stands in. Showing "Alice, and 4 others" when four of them are
    /// named nowhere is worse than saying how many there are.
    #[test]
    fn an_unnamed_membership_counts_instead() {
        assert_eq!(resolve(None, None, &[], 5), "5 people");
    }

    /// Alone in a room: there is no one to name it after. Empty on purpose, so
    /// `with_fallback` takes over and the sidebar shows the room id — which is
    /// also how matrix-nio presented it.
    #[test]
    fn being_alone_yields_no_name() {
        assert_eq!(resolve(None, None, &["Alice"], 1), "");
        assert_eq!(resolve(None, None, &[], 1), "");
    }

    /// Sorting is not cosmetic. The same two members arriving in a different
    /// order between two syncs must not produce two different labels for one
    /// room, or the sidebar appears to contain a duplicate.
    #[test]
    fn the_order_of_the_members_does_not_change_the_name() {
        assert_eq!(
            from_heroes(&["Bob", "Alice"], 3),
            from_heroes(&["Alice", "Bob"], 3)
        );
    }

    /// Synapse resends the same hero on several syncs; a repeated name must not
    /// turn "Alice, Alice" into a room called that.
    #[test]
    fn a_repeated_name_is_not_listed_twice() {
        assert_eq!(from_heroes(&["Alice", "Alice"], 2), "Alice");
    }
}
