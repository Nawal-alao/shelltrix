"""Tests for rendering the conversation timeline (grouping by sender,
time and date separators, replies, reactions).

Targets the pure logic of `shelltrix.formatting` (`format_timeline_blocks` /
`format_timeline_entries`, replies, dates, reactions), with no Textual state.
"""

from __future__ import annotations

from datetime import date, timedelta

from shelltrix.formatting import (
    TIME_GAP_SEPARATOR_MS,
    annotation_of,
    TimelineContext,
    TimelineEntry,
    body_mentions_user,
    day_label,
    format_date_separator,
    format_timeline_blocks,
    format_timeline_entries,
    highlight_mentions,
    interval_time_gap,
    needs_date_separator,
    reaction_counts,
    reaction_summary,
    reply_fallback,
    reply_quote_line,
    reply_target_of,
    strip_reply_fallback,
    weekday_name,
)


def entry(
    sender: str,
    body: str,
    *,
    is_own: bool = False,
    time_ms: int = 0,
    name: str | None = None,
) -> TimelineEntry:
    return TimelineEntry(
        sender=sender,
        display_name=name or sender,
        is_own=is_own,
        time_ms=time_ms,
        body=body,
        timestamp="HH:MM",
    )


def head(e: TimelineEntry) -> str:
    return f"HEAD({'<me>' if e.is_own else e.sender})"


def render(entries, ctx: TimelineContext | None = None) -> tuple[list[str], TimelineContext]:
    lines, new_ctx = format_timeline_entries(
        entries, ctx or TimelineContext(), header_for=head
    )
    return lines, new_ctx


class TestIntervalTimeGap:
    def test_below_threshold_no_separator(self) -> None:
        assert interval_time_gap(0, TIME_GAP_SEPARATOR_MS - 1) is False

    def test_at_threshold_separates(self) -> None:
        assert interval_time_gap(0, TIME_GAP_SEPARATOR_MS) is True

    def test_well_beyond_separates(self) -> None:
        assert interval_time_gap(0, TIME_GAP_SEPARATOR_MS * 10) is True


class TestFormatTimelineEntries:
    def test_single_entry_opens_block(self) -> None:
        lines, ctx = render([entry("@a:hs", "hi")])
        assert lines == ["        HEAD(@a:hs)", "        hi"]
        assert ctx.last_sender == "@a:hs"

    def test_same_sender_continuation(self) -> None:
        """Two consecutive messages from the same sender → a single block."""
        lines, ctx = render(
            [entry("@a:hs", "one"), entry("@a:hs", "two")]
        )
        assert lines == [
            "        HEAD(@a:hs)",
            "        one",
            "        two",
        ]

    def test_different_sender_new_block(self) -> None:
        lines, _ = render(
            [entry("@a:hs", "one"), entry("@b:hs", "two")]
        )
        # One header per sender; no name repeated on the body lines
        assert lines == [
            "        HEAD(@a:hs)",
            "        one",
            "        HEAD(@b:hs)",
            "        two",
        ]

    def test_same_sender_time_gap_separator(self) -> None:
        """Silence > 5 min → time separator + new block."""
        t0 = 1_700_000_000_000
        entries = [
            entry("@a:hs", "before", time_ms=t0),
            entry("@a:hs", "later", time_ms=t0 + TIME_GAP_SEPARATOR_MS + 1),
        ]
        lines, _ = render(entries)
        # A time separator appears before the next message
        sep_lines = [ln for ln in lines if "─" in ln]
        assert sep_lines, "time separator missing"
        # ... then a new block header for the same sender
        assert lines[-2] == "        HEAD(@a:hs)"
        assert lines[-1] == "        later"

    def test_sender_returns_after_another_opens_new_block(self) -> None:
        """A, B, A: A's return opens a new block."""
        lines, _ = render(
            [
                entry("@a:hs", "1"),
                entry("@b:hs", "2"),
                entry("@a:hs", "3"),
            ]
        )
        a_headers = [ln for ln in lines if ln.endswith("HEAD(@a:hs)")]
        assert len(a_headers) == 2
        assert lines[-1] == "        3"

    def test_own_message_indicator(self) -> None:
        lines, _ = render([entry("@me:hs", "salut", is_own=True)])
        assert lines[0] == "        HEAD(<me>)"

    def test_continues_existing_context(self) -> None:
        """Incremental rendering starts from the provided context."""
        ctx = TimelineContext(last_sender="@a:hs", last_time_ms=100)
        lines, new_ctx = render([entry("@a:hs", "more", time_ms=100)], ctx=ctx)
        # Continuation: no new header
        assert lines == ["        more"]
        assert new_ctx.last_sender == "@a:hs"

    def test_category_separator_resets_group(self) -> None:
        """A sender change after a return creates a header."""
        ctx = TimelineContext(last_sender="@b:hs", last_time_ms=100)
        lines, _ = render([entry("@a:hs", "new", time_ms=100)], ctx=ctx)
        assert lines[0] == "        HEAD(@a:hs)"


class TestBodyMentionsUser:
    """Tests for body_mentions_user()."""

    def test_matches_full_user_id(self) -> None:
        assert body_mentions_user("regarde @alice:matrix.org stp", "@alice:matrix.org")

    def test_matches_localpart(self) -> None:
        assert body_mentions_user("hey @alice can you?", "@alice:matrix.org")

    def test_no_mention(self) -> None:
        assert not body_mentions_user("salut tout le monde", "@alice:matrix.org")

    def test_empty_body(self) -> None:
        assert not body_mentions_user("", "@alice:matrix.org")

    def test_empty_user_id(self) -> None:
        assert not body_mentions_user("salut @alice", "")

    def test_similar_partial_name_not_mentioned(self) -> None:
        assert not body_mentions_user("talk to Alice in general", "@alice:matrix.org")


class TestHighlightMentions:
    """Tests for highlight_mentions(): mention highlighting."""

    @staticmethod
    def _accent() -> str:
        from shelltrix import themes

        return themes.accent()

    def test_full_user_id_highlighted(self) -> None:
        a = self._accent()
        out = highlight_mentions("bonjour @alice:matrix.org !", "@alice:matrix.org")
        assert f"[bold][{a}]@alice:matrix.org[/{a}][/bold]" in out

    def test_localpart_highlighted(self) -> None:
        a = self._accent()
        out = highlight_mentions("hey @alice can you?", "@alice:matrix.org")
        assert f"[bold][{a}]@alice[/{a}][/bold]" in out

    def test_ignored_when_no_user_id(self) -> None:
        assert highlight_mentions("@alice", "") == "@alice"

    def test_ignored_when_no_mention(self) -> None:
        assert highlight_mentions("juste du texte", "@alice:matrix.org") == "juste du texte"

    def test_partial_name_not_highlighted(self) -> None:
        a = self._accent()
        out = highlight_mentions("@alice2 vient", "@alice:matrix.org")
        assert "@alice2" in out
        assert f"[{a}]@alice2" not in out

    def test_other_server_not_highlighted(self) -> None:
        a = self._accent()
        out = highlight_mentions("@alice:autreserveur ici", "@alice:matrix.org")
        assert f"[{a}]@alice:autreserveur" not in out
        assert "@alice:autreserveur" in out


class TestMessageBlocks:
    """Block rendering (one widget per message) and its separators."""

    def _blocks(self, entries):
        return format_timeline_blocks(entries, TimelineContext(), header_for=head)

    def test_one_block_per_entry(self) -> None:
        blocks, _ = self._blocks([entry("@a:hs", "un"), entry("@b:hs", "deux")])
        assert [b.entry.body for b in blocks] == ["un", "deux"]
        assert len(blocks) == 2

    def test_continuation_has_no_header(self) -> None:
        blocks, _ = self._blocks([entry("@a:hs", "un"), entry("@a:hs", "deux")])
        assert blocks[0].is_continuation is False
        assert blocks[1].is_continuation is True
        # No duplicated header on the continuation
        assert all("HEAD(" not in ln for ln in blocks[1].lines)

    def test_gap_before_only_after_silence(self) -> None:
        t0 = 1_700_000_000_000
        blocks, _ = self._blocks(
            [
                entry("@a:hs", "avant", time_ms=t0),
                entry("@a:hs", "apres", time_ms=t0 + TIME_GAP_SEPARATOR_MS + 1),
            ]
        )
        assert blocks[0].gap_before is False
        assert blocks[1].gap_before is True

    def test_date_separator_on_day_change(self) -> None:
        jour = 24 * 60 * 60 * 1000
        t0 = 1_700_000_000_000
        blocks, _ = self._blocks(
            [
                entry("@a:hs", "jour 1", time_ms=t0),
                entry("@a:hs", "jour 2", time_ms=t0 + jour),
            ]
        )
        assert blocks[0].date_before is False
        assert blocks[1].date_before is True

    def test_date_change_alone_triggers_separator(self) -> None:
        """Midnight: 2 minutes apart, but the day changes."""
        minuit = 24 * 60 * 60 * 1000
        t0 = 1_700_000_000_000
        blocks, _ = self._blocks(
            [
                entry("@a:hs", "23h59", time_ms=t0),
                entry("@a:hs", "00h01", time_ms=t0 + minuit - 120_000 + 120_000),
            ]
        )
        # 24h apart: both markers can be present
        assert blocks[1].date_before is True
        assert blocks[1].gap_before is True

    def test_flatten_matches_blocks(self) -> None:
        """The flattened view and the blocks must stay consistent."""
        t0 = 1_700_000_000_000
        entries = [
            entry("@a:hs", "un", time_ms=t0),
            entry("@a:hs", "deux", time_ms=t0 + 1000),
            entry("@b:hs", "trois", time_ms=t0 + TIME_GAP_SEPARATOR_MS + 1),
        ]
        blocks, _ = format_timeline_blocks(entries, TimelineContext(), header_for=head)
        lines, _ = format_timeline_entries(entries, TimelineContext(), header_for=head)
        expected = []
        for b in blocks:
            if b.gap_before:
                expected += ["", f"[dim]HH:MM {'─' * 36}[/dim]"]
            expected += b.lines
        assert lines == expected


class TestReplies:
    def test_quote_absent_without_reply(self) -> None:
        assert reply_quote_line(entry("@a:hs", "coucou")) == ""

    def test_quote_names_the_cited_person(self) -> None:
        e = entry("@a:hs", "coucou")
        e.reply_to_name = "Tim"
        assert "Tim" in reply_quote_line(e)
        assert "┌─" in reply_quote_line(e)

    def test_quote_precedes_body(self) -> None:
        e = entry("@a:hs", "coucou")
        e.reply_to_name = "Tim"
        blocks, _ = format_timeline_blocks([e], TimelineContext(), header_for=head)
        lines = blocks[0].lines
        assert "Tim" in lines[-2], "the quote must come before the body"
        assert lines[-1].endswith("coucou")

    def test_reply_name_is_escaped(self) -> None:
        e = entry("@a:hs", "x")
        e.reply_to_name = "[bold]trap"
        out = reply_quote_line(e)
        # The bracket must be escaped (Rich: `\[`), otherwise a malicious
        # display_name would inject itself as a tag and disguise the message.
        assert "\\[bold]trap" in out
        # no UNESCAPED tag must remain
        assert "[bold]" not in out.replace("\\[", "")

    def test_target_from_modern_form(self) -> None:
        content = {
            "m.relates_to": {"rel_type": "m.in_reply_to", "event_id": "$abc"}
        }
        assert reply_target_of(content) == "$abc"

    def test_target_from_legacy_form(self) -> None:
        assert reply_target_of({"m.in_reply_to": {"event_id": "$old"}}) == "$old"

    def test_no_target_when_plain_message(self) -> None:
        assert reply_target_of({"body": "coucou", "msgtype": "m.text"}) == ""

    def test_annotation_is_not_a_reply(self) -> None:
        content = {
            "m.relates_to": {"rel_type": "m.annotation", "event_id": "$x", "key": "👍"}
        }
        assert reply_target_of(content) == ""

    def test_fallback_round_trip(self) -> None:
        """What we send must be strippable on receipt."""
        sent = reply_fallback("je suis d'accord", "@tim:hs")
        assert sent == "<@tim:hs> je suis d'accord"
        assert strip_reply_fallback(sent, "@tim:hs") == "je suis d'accord"

    def test_fallback_kept_for_other_author(self) -> None:
        """A prefix from ANOTHER author is not a fallback: we leave it alone."""
        body = "<@alice:hs> bonjour"
        assert strip_reply_fallback(body, "@tim:hs") == body

    def test_fallback_without_author(self) -> None:
        assert reply_fallback("texte", "") == "texte"
        assert strip_reply_fallback("texte", "") == "texte"


class TestDateSeparators:
    def test_labels(self) -> None:
        jour = 24 * 60 * 60 * 1000
        now = 1_700_000_000_000
        assert day_label(now, now_ms=now) == "Today"
        assert day_label(now - jour, now_ms=now) == "Yesterday"

    def test_old_date_is_numeric(self) -> None:
        now = 1_700_000_000_000
        old = day_label(now - 400 * 24 * 60 * 60 * 1000, now_ms=now)
        assert "/" in old and not old.isalpha()

    def test_recent_days_use_weekday_names(self) -> None:
        # `strftime("%A")` would follow the process locale and could
        # disagree with the UI; weekday names are pinned like the rest of it.
        # 2024-01-01 is a Monday.
        lundi = date(2024, 1, 1)
        assert [
            weekday_name(lundi + timedelta(days=i))
            for i in range(7)
        ] == [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]

    def test_separator_line_has_the_label(self) -> None:
        sep = format_date_separator(1_700_000_000_000)
        assert sep.startswith("[dim]─── ") and sep.endswith("[/dim]")

    def test_no_separator_without_timestamps(self) -> None:
        assert needs_date_separator(0, 0) is False
        assert needs_date_separator(0, 1_700_000_000_000) is False
        assert needs_date_separator(1_700_000_000_000, 0) is False

    def test_same_day_no_separator(self) -> None:
        base = 1_700_000_000_000
        assert needs_date_separator(base, base + 1000) is False


class TestReactionSummary:
    def test_empty(self) -> None:
        assert reaction_summary({}) == ""

    def test_single_reaction_shows_count(self) -> None:
        assert "1" in reaction_summary({"👍": 1})

    def test_sorted_by_count_desc(self) -> None:
        out = reaction_summary({"a": 1, "b": 5, "c": 3})
        assert out.index("b") < out.index("c") < out.index("a")

    def test_ties_are_stable(self) -> None:
        out = reaction_summary({"b": 2, "a": 2})
        assert out.index("a") < out.index("b")

    def test_keys_are_escaped(self) -> None:
        out = reaction_summary({"[bold]x": 2})
        assert "\\[bold]x" in out
        assert "[bold]" not in out.replace("\\[", "")


class TestAnnotationOf:
    def test_reads_target_and_key(self) -> None:
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$m1",
                "key": "👍",
            }
        }
        assert annotation_of(content) == ("$m1", "👍")

    def test_reply_is_not_an_annotation(self) -> None:
        """Replies and reactions share `m.relates_to`: only rel_type tells them apart."""
        content = {
            "m.relates_to": {
                "rel_type": "m.in_reply_to",
                "event_id": "$m1",
            }
        }
        assert annotation_of(content) == ("", "")

    def test_edit_is_not_an_annotation(self) -> None:
        content = {
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": "$m1",
                "key": "* nouveau texte",
            }
        }
        assert annotation_of(content) == ("", "")

    def test_plain_message(self) -> None:
        assert annotation_of({"msgtype": "m.text", "body": "coucou"}) == ("", "")

    def test_malformed_annotation_is_ignored(self) -> None:
        base = {"rel_type": "m.annotation"}
        assert annotation_of({"m.relates_to": {**base, "key": "👍"}}) == ("", "")
        assert annotation_of({"m.relates_to": {**base, "event_id": "$m1"}}) == ("", "")
        assert annotation_of({"m.relates_to": {**base, "event_id": 7, "key": 3}}) == ("", "")
        assert annotation_of({"m.relates_to": "pas un dict"}) == ("", "")
        assert annotation_of({}) == ("", "")


class TestReactionCounts:
    def test_counts_per_key(self) -> None:
        assert reaction_counts({"@a:hs": "👍", "@b:hs": "👍", "@c:hs": "❤️"}) == {
            "👍": 2,
            "❤️": 1,
        }

    def test_empty(self) -> None:
        assert reaction_counts({}) == {}

    def test_one_sender_never_counts_twice(self) -> None:
        """The spec allows only one reaction per author and per message."""
        assert reaction_counts({"@a:hs": "👍"}) == {"👍": 1}

    def test_change_of_mind_replaces_the_key(self) -> None:
        by_sender = {"@a:hs": "❤️", "@b:hs": "👍"}
        assert reaction_counts(by_sender) == {"❤️": 1, "👍": 1}
