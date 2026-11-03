"""Reader-to-author affinity (#117): the helpers, the scoring change and where the ranker
applies it. The query runs against a real database in tests/integration/test_affinity_db.py.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from haf_fyp import affinity
from haf_fyp.scoring import compute_scores

V14 = {
    "relevance": 0.40, "recency": 0.30, "engagement": 0.20, "credibility": 0.10,
    "community_boost": 0.20, "community_boost_engaged": 0.10, "boost_rescale": True,
    "author_affinity": 0.30, "affinity_days": 30, "affinity_half_life_days": 7,
    "affinity_half_at": 3, "affinity_max_authors": 200,
}
V13 = {k: v for k, v in V14.items() if not (k == "author_affinity" or k.startswith("affinity_"))}


# ---- squash / settings

@pytest.mark.parametrize("raw, want", [(0.0, 0.0), (-1.0, 0.0), (3.0, 0.5), (9.0, 0.75)])
def test_squash(raw, want):
    assert affinity.squash(raw, 3.0) == pytest.approx(want)


def test_squash_never_reaches_one():
    assert affinity.squash(1e9, 3.0) < 1.0


def test_settings_take_the_versions_own_and_default_the_rest():
    s = affinity.settings({"affinity_days": 14, "relevance": 0.4})
    assert s == {**affinity.DEFAULTS, "affinity_days": 14}


# ---- the read and the write

def _conn(rows=()):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = list(rows)
    return conn, cur


def test_the_read_is_asked_for_the_reader_with_the_versions_settings():
    conn, cur = _conn([("bob", 3.0), ("carol", 1.0)])
    got = affinity.author_affinity(conn, "alice", {**V14, "affinity_days": 14})
    query, params = cur.execute.call_args.args
    assert params == {"username": "alice", "days": 14, "half_life": 7.0, "max_authors": 200,
                      "comment_weight": affinity.COMMENT_WEIGHT}
    assert got == {"bob": (0.5, 3.0), "carol": (0.25, 1.0)}
    # Votes and reblogs from haf_fyp's own copy; comments from hivemind; never the reader.
    assert "haf_fyp_app.user_interactions AS ui" in query
    assert "parent.id = c.parent_id" in query and "c.author_id = (SELECT id FROM reader)" in query
    assert "a.author_id <> (SELECT id FROM reader)" in query


def test_the_write_replaces_the_readers_set(monkeypatch):
    conn, cur = _conn()
    inserted = []
    monkeypatch.setattr(affinity.psycopg2.extras, "execute_values",
                        lambda _cur, sql, rows, template: inserted.append((sql, rows)))
    affinity.write_author_affinity(conn, "alice", {"bob": (0.5, 3.0)})
    assert cur.execute.call_args.args == (
        "DELETE FROM haf_fyp_app.user_author_affinity WHERE username = %s", ("alice",))
    assert "INSERT INTO haf_fyp_app.user_author_affinity" in inserted[0][0]
    assert inserted[0][1] == [("alice", "bob", 0.5, 3.0)]
    conn.commit.assert_called_once()


def test_an_empty_set_still_clears_the_old_one():
    conn, cur = _conn()
    affinity.write_author_affinity(conn, "alice", {})
    assert cur.execute.call_count == 1
    assert "DELETE" in cur.execute.call_args.args[0]
    conn.commit.assert_called_once()


# ---- scoring

def _feed():
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    return pd.DataFrame([
        {"post_id": i, "author": a, "permlink": f"p{i}", "community_id": None, "created_at": now,
         "unique_commenters": 1, "reblog_count": 0, "net_rshares": 0, "vote_count": 1}
        for i, a in enumerate(["liked", "stranger", "liked2"])
    ])


# Post 0 and post 1 equally relevant, post 2 a little more.
_VECS = {0: np.array([1.0, 0.0]), 1: np.array([1.0, 0.0]), 2: np.array([1.0, 0.1])}
_USER = np.array([0.6, 0.8])


def _scores(weights, aff, user=_USER, vecs=_VECS):
    return compute_scores(candidates=_feed(), user_vector=user, post_vectors=vecs,
                          author_credibility={}, subscribed_communities=set(),
                          engaged_communities=set(), algorithm_weights=weights,
                          author_affinity=aff).set_index("post_id")


def test_affinity_raises_relevance_by_its_share_of_the_headroom():
    base = _scores(V13, None)
    got = _scores(V14, {"liked": (0.5, 3.0)})
    r = base.loc[0, "score_relevance"]
    assert got.loc[0, "score_relevance"] == pytest.approx(r + 0.30 * (1 - r) * 0.5)
    assert got.loc[0, "score_affinity"] == 0.5
    assert got.loc[0, "final_score"] > base.loc[0, "final_score"]


def test_an_author_the_reader_never_engaged_with_scores_as_before():
    base = _scores(V13, None)
    got = _scores(V14, {"liked": (0.5, 3.0)})
    assert got.loc[1, "score_relevance"] == pytest.approx(base.loc[1, "score_relevance"])
    assert got.loc[1, "final_score"] == pytest.approx(base.loc[1, "final_score"])
    assert got.loc[1, "score_affinity"] == 0.0


def test_affinity_reorders_equally_relevant_posts():
    order = list(_scores(V14, {"liked": (0.9, 27.0)}).sort_values("final_score", ascending=False).index)
    assert order.index(0) < order.index(1)


def test_relevance_stays_at_most_one():
    got = _scores(V14, {"liked": (0.999, 1e6), "liked2": (0.999, 1e6)}, user=np.array([1.0, 0.0]))
    assert (got["score_relevance"] <= 1.0).all()


def test_a_version_without_the_key_ignores_the_affinity_it_is_given():
    base = _scores(V13, None)
    got = _scores(V13, {"liked": (0.9, 27.0)})
    assert np.allclose(got["final_score"], base["final_score"])
    assert (got["score_affinity"] == 0.0).all()


def test_cold_start_drops_affinity_with_relevance():
    # No user vector: the relevance weight is dropped, and affinity, which rides on it,
    # with it -- documented in compute_scores.
    base = _scores(V13, None, user=None)
    got = _scores(V14, {"liked": (0.9, 27.0)}, user=None)
    assert np.allclose(got["final_score"], base["final_score"])
    # What feed_cache stores and the API serves is not raised either.
    assert np.allclose(got["score_relevance"], base["score_relevance"])
    assert (got["score_affinity"] == 0.0).all()


def test_the_versions_half_at_sets_the_scale():
    conn, _cur = _conn([("bob", 3.0)])
    assert affinity.author_affinity(conn, "alice", {"affinity_half_at": 1})["bob"][0] == pytest.approx(0.75)


# ---- where the ranker applies it

rd = pytest.importorskip("haf_fyp.ranker_daemon")


def _rank(monkeypatch, weights):
    seen = {}
    monkeypatch.setattr(rd.visibility, "muted_authors", lambda _c, _u: set())
    monkeypatch.setattr(rd.visibility, "blacklisted_authors", lambda _c, _u: set())
    monkeypatch.setattr(rd, "fetch_user_communities", lambda _c, _u: set())
    monkeypatch.setattr(rd, "fetch_user_interests", lambda _c, _u: set())
    monkeypatch.setattr(rd, "fetch_top_communities", lambda _c, _u: [])
    monkeypatch.setattr(rd, "build_user_profile_vector", lambda _c, _u, _v: (None, 0))
    monkeypatch.setattr(rd, "read_user_vector", lambda _c, _u: (None, None))
    monkeypatch.setattr(rd.affinity, "author_affinity",
                        lambda _c, u, w: seen.setdefault("read", (u, w)) and {"bob": (0.5, 3.0)})
    monkeypatch.setattr(rd.affinity, "write_author_affinity",
                        lambda _c, u, a: seen.setdefault("written", (u, a)))
    monkeypatch.setattr(rd, "compute_scores", lambda **kw: seen.setdefault("kw", kw) and pd.DataFrame())
    monkeypatch.setattr(rd, "write_feed_cache", lambda *a, **k: None)
    rd._rank_user(MagicMock(), "alice", _feed(), {}, {}, "x", weights)
    return seen


def test_a_version_with_affinity_reads_stores_and_scores_it(monkeypatch):
    seen = _rank(monkeypatch, V14)
    assert seen["read"] == ("alice", V14)
    assert seen["written"] == ("alice", {"bob": (0.5, 3.0)})
    assert seen["kw"]["author_affinity"] == {"bob": (0.5, 3.0)}


def test_a_version_without_it_neither_reads_nor_stores(monkeypatch):
    seen = _rank(monkeypatch, V13)
    assert "read" not in seen and "written" not in seen
    assert seen["kw"]["author_affinity"] is None
