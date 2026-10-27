"""Read sessions (#104): a feed is not rewritten while its owner scrolls it.

The database side (the stamps, the refusal in write_feed_cache, the shipped feed and
warm-up functions) is in tests/integration/test_reader_first_db.py and
test_feed_cache_format_db.py. This file pins what the ranker does with an account it
may not rewrite: it keeps it, and asks again.

The helpers of test_reader_first.py are reused, as the other reader tests do.
"""
import collections
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from haf_fyp import ranker_daemon as rd
from haf_fyp.scoring import FeedSessionOpen

from .test_reader_first import _Clock, _drive_main, _mock_conn, _reader_state  # noqa: F401


def _note(payload, channel):
    return SimpleNamespace(channel=channel, payload=payload)


# ---- the two signals ------------------------------------------------------------


def test_a_warmup_is_told_apart_from_a_read() -> None:
    conn = MagicMock()
    conn.notifies = collections.deque([
        _note("alice", rd.FEED_READ_CHANNEL),
        _note("bobby", rd.FEED_WARMUP_CHANNEL),
        _note("carol", rd.FEED_WARMUP_CHANNEL),
        _note("Not-A-Name", rd.FEED_WARMUP_CHANNEL),
    ])

    assert rd._drain_feed_reads(conn) == ({"alice"}, {"bobby", "carol"})


def test_only_a_read_opens_a_session() -> None:
    conn, cur = _mock_conn([[]])

    rd._note_feed_reads(conn, {"alice"}, 10, warmups={"bobby"})

    (call,) = cur.execute.call_args_list
    reading, everyone = call.args[1][2], call.args[1][3]
    assert reading == ["alice"]
    assert everyone == ["alice", "bobby"]  # both stamped: a warm-up is rationed too
    sql = " ".join(call.args[0].split())
    assert "feed_session_until = CASE WHEN picked.reading" in sql


def test_a_warmup_alone_is_stamped() -> None:
    conn, cur = _mock_conn([[("bobby", None)]])

    assert rd._note_feed_reads(conn, set(), 10, warmups={"bobby"}) == {"bobby"}
    cur.execute.assert_called_once()
    conn.commit.assert_called_once()


def test_listening_covers_the_warmup_channel(monkeypatch) -> None:
    conn, cur = _mock_conn([], fetchone=(True,))

    rd._listen_for_feed_reads(conn)

    statements = [c.args[0] for c in cur.execute.call_args_list]
    assert f"LISTEN {rd.FEED_READ_CHANNEL}" in statements
    assert f"LISTEN {rd.FEED_WARMUP_CHANNEL}" in statements


# ---- the main loop keeps what was held ---------------------------------------------


def test_a_rerank_held_for_a_session_is_asked_about_on_every_pass_until_it_runs(monkeypatch) -> None:
    # The refresh that held it fetched no candidates, so the 30 s that space refreshes
    # out are not waited: it runs on the first pass after the session ends.
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=4,
        drain=[{"a"}, set(), set(), set()], note=[{"a"}],
        held=[{"a"}, {"a"}],
    )

    assert [names for _conn, names in seen.targeted] == [{"a"}, {"a"}, {"a"}]


def test_an_opt_in_held_for_a_session_is_not_dropped(monkeypatch) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=2, drain=[set(), set()], note=[],
        queued=["carol"], held=[{"carol"}],
    )

    assert [names for _conn, names in seen.targeted] == [{"carol"}, {"carol"}]


def test_a_refresh_that_ranked_someone_still_spaces_the_next_one_out(monkeypatch) -> None:
    monkeypatch.setattr(rd, "READ_REFRESH_BATCH_SECONDS", 30)
    seen = _drive_main(
        monkeypatch, clock=_Clock(), passes=3,
        drain=[{"a"}, set(), set()], note=[{"a", "b"}], held=[{"a"}],
    )

    # b was ranked, so a waits the 30 s like any reader that came due after a refresh.
    assert [names for _conn, names in seen.targeted] == [{"a", "b"}]


# ---- the targeted refresh ----------------------------------------------------------


def _refresh(monkeypatch, names, *, in_session=(), rank=None):
    conn, _ = _mock_conn([])
    ranked, reconciled = [], []

    def _rank(_c, username, *_a, **_k):
        if rank is not None:
            rank(username)
        ranked.append(username)

    def _evict(_c, users):
        reconciled.extend(users)
        return set()

    monkeypatch.setattr(rd, "_in_session", lambda _c, users: set(users) & set(in_session))
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", _evict)
    monkeypatch.setattr(rd, "_rotation_members", lambda _c, u: set(u))
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "_rank_user", _rank)
    held = rd._run_targeted_refresh(conn, set(names))
    return held, ranked, reconciled


def test_a_refresh_leaves_an_account_in_a_session_alone_and_says_so(monkeypatch) -> None:
    held, ranked, reconciled = _refresh(monkeypatch, {"alice", "bobby"}, in_session={"alice"})

    assert held == {"alice"}
    assert ranked == ["bobby"]
    # Not reconciled either: evicting an un-opted-in reader deletes the feed being read.
    assert reconciled == ["bobby"]


def test_a_refresh_with_everyone_in_a_session_fetches_nothing(monkeypatch) -> None:
    monkeypatch.setattr(rd, "_fetch_candidates", lambda _c: (_ for _ in ()).throw(AssertionError))
    conn, _ = _mock_conn([])
    monkeypatch.setattr(rd, "_in_session", lambda _c, users: set(users))
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)

    assert rd._run_targeted_refresh(conn, {"alice"}) == {"alice"}


def test_a_session_opened_during_the_refresh_is_held_not_failed(monkeypatch, caplog) -> None:
    def _rank(username):
        if username == "alice":
            raise FeedSessionOpen(username)

    monkeypatch.setattr(rd, "_log_user_failure", lambda *_a: (_ for _ in ()).throw(AssertionError))
    with caplog.at_level(logging.WARNING):
        held, ranked, _ = _refresh(monkeypatch, {"alice", "bobby"}, rank=_rank)

    assert held == {"alice"}
    assert ranked == ["bobby"]
    assert "failed" not in caplog.text


# ---- the scheduled cycle -----------------------------------------------------------


def _cycle(monkeypatch, users, rank, reads=None):
    conn, _ = _mock_conn([])
    monkeypatch.setattr(rd, "register_vector_type", lambda _c: None)
    monkeypatch.setattr(rd, "_evict_unsubscribed", lambda _c, _u: set())
    monkeypatch.setattr(rd, "_rotation_members", lambda _c, u: set(u))
    monkeypatch.setattr(rd, "_fetch_personalised_users", lambda _c, _limit: list(users))
    monkeypatch.setattr(rd, "_fetch_algorithm", lambda _c: ("1.0.0", {}))
    monkeypatch.setattr(
        rd, "_fetch_candidates", lambda _c: pd.DataFrame({"post_id": [1], "author": ["a"]})
    )
    monkeypatch.setattr(rd, "fetch_post_vectors_safe", lambda *a, **kw: {})
    monkeypatch.setattr(rd, "_fetch_author_credibility_safe", lambda _c, _a: {})
    monkeypatch.setattr(rd, "write_global_feed_cache", lambda *a, **k: None)
    monkeypatch.setattr(rd, "compute_scores", lambda **k: pd.DataFrame())
    monkeypatch.setattr(rd, "_rank_user", rank)
    return rd.run_cycle(conn, reads)


def test_a_cycle_holds_an_account_whose_session_opened_after_it_was_chosen(monkeypatch, caplog) -> None:
    def _rank(_c, username, *_a, **_k):
        if username == "bobby":
            raise FeedSessionOpen(username)

    monkeypatch.setattr(rd, "_log_user_failure", lambda *_a: (_ for _ in ()).throw(AssertionError))
    with caplog.at_level(logging.INFO):
        ranked = _cycle(monkeypatch, ["alice", "bobby", "carol"], _rank)

    # Not ranked, so main() does not take it off the waiting list; not a failure either.
    assert ranked == {"alice", "carol"}
    assert "refreshed personalized feeds for 2/3 cached users (0 failed, 1 held for a read session)" in caplog.text


def test_a_cycle_does_not_take_a_reader_in_a_session_ahead_of_its_turn(monkeypatch) -> None:
    order = []
    monkeypatch.setattr(rd, "_in_session", lambda _c, users: {"zed"} & set(users))
    monkeypatch.setattr(rd, "POLL_SECONDS", 0)
    answers = [{"zed", "yan"}]

    ranked = _cycle(
        monkeypatch, ["alice", "zed"],
        lambda _c, username, *_a, **_k: order.append(username),
        reads=lambda _c: answers.pop(0) if answers else set(),
    )

    # yan's session is over: ranked first. zed's is open: turned away, and kept out of
    # the rest of the queue it was already in.
    assert order == ["yan", "alice"]
    assert ranked == {"yan", "alice"}
