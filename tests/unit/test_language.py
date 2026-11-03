"""haf_fyp.language (#115): what the detector reads, which languages a post keeps, and the
best-effort database step. The detector itself is py3langid's; the texts here are plain
enough that any model version worth pinning tells them apart."""

import logging
from unittest.mock import MagicMock

import psycopg2
import pytest

from haf_fyp import language

EN = (
    "Ten minutes is enough to start",
    "Instead of trying to run ten kilometres every day, I started with a ten minute walk each "
    "morning, and little by little it became a habit that I now enjoy before work.",
)
ES = (
    "Diez minutos bastan para empezar",
    "En lugar de intentar correr diez kilómetros todos los días, empecé con diez minutos de "
    "caminata cada mañana y poco a poco se volvió un hábito que ahora disfruto antes del trabajo.",
)
DE = (
    "Mein Wochenende in Berlin",
    "Am Samstag waren wir im Park spazieren und haben danach in einem kleinen Café Kuchen "
    "gegessen. Das Wetter war wunderbar und wir sind erst am Abend nach Hause gegangen.",
)


@pytest.fixture(autouse=True)
def _real_identifier():
    """Tests that swap in a fake identifier must not leave it for the next one."""
    saved = language._identifier
    yield
    language._identifier = saved


# ---- what the detector reads


def test_markup_links_accounts_and_hashtags_are_not_read():
    text = language.text_of("T", "![img](http://x/y.png) [link text](http://a) <b>bold</b> @alice #tag see https://x.y/z")
    assert text == "T. link text bold see"


def test_the_text_is_cut_at_twice_max_chars():
    assert len(language.text_of("t", "word " * 5000)) == 2 * language.MAX_CHARS


def test_the_whole_text_language_is_read_from_the_first_max_chars():
    seen = []

    class _Rank(_Fake):
        def rank(self, text):
            seen.append(text)
            return [("en", 0.99)]

    _with(_Rank([]))
    language.languages_of("t", "word " * 5000)
    assert len(seen[0]) == language.MAX_CHARS


# ---- the languages a post keeps


@pytest.mark.parametrize("post, want", [(EN, ["en"]), (ES, ["es"]), (DE, ["de"])])
def test_a_post_in_one_language_gets_that_language(post, want):
    assert language.languages_of(*post) == want


def test_a_post_written_twice_gets_both_languages():
    """The "[ENG|ESP]" post: the whole of it in English, then again in Spanish. Over the
    whole text one language wins; a window over the other half finds the second."""
    body = " ".join([EN[1]] * 4) + "\n\n---\n\n" + " ".join([ES[1]] * 4)
    assert sorted(language.languages_of("Ten minutes / Diez minutos", body)) == ["en", "es"]


def test_a_long_post_written_twice_still_gets_its_second_language():
    """The English version alone is longer than MAX_CHARS: the windows still reach the
    Spanish one after it."""
    english = " ".join([EN[1]] * 40)
    assert len(english) > language.MAX_CHARS
    body = english + "\n\n---\n\n" + " ".join([ES[1]] * 40)
    assert language.languages_of("Ten minutes / Diez minutos", body) == ["en", "es"]


@pytest.mark.parametrize("title, body", [
    ("", ""),
    (None, None),
    ("![x](https://img/a.jpg)", "https://3speak.tv/watch?v=abc @someone #hive"),
])
def test_a_post_with_nothing_to_read_gets_no_language(title, body):
    assert language.languages_of(title, body) == []


class _Fake:
    """An identifier that answers what a test tells it to."""

    def __init__(self, ranked, windows=None):
        self.ranked, self.windows, self.asked = ranked, list(windows or []), []

    def rank(self, text):
        return self.ranked

    def classify(self, text):
        self.asked.append(text)
        return self.windows.pop(0) if self.windows else ("en", 1.0)


def _with(fake):
    language._identifier = fake
    return fake


LONG = "x" * (3 * language.WINDOW_CHARS)


def test_a_runner_up_at_min_share_is_kept_and_no_window_is_read():
    fake = _with(_Fake([("es", 0.6), ("en", language.MIN_SHARE), ("pt", 0.1)]))
    assert language.languages_of("t", LONG) == ["es", "en"]
    assert fake.asked == []


def test_a_runner_up_below_min_share_is_not():
    _with(_Fake([("es", 0.9), ("en", 0.09)], windows=[("es", 1.0)] * language.WINDOWS))
    assert language.languages_of("t", LONG) == ["es"]


def test_a_first_language_below_min_share_means_none():
    _with(_Fake([("en", 0.29), ("es", 0.28)]))
    assert language.languages_of("t", LONG) == []


def test_labels_that_are_no_language_are_dropped():
    _with(_Fake([("zxx", 0.9), ("en", 0.1)]))
    assert language.languages_of("t", LONG) == []
    _with(_Fake([("en", 0.95)], windows=[("und", 1.0), ("zxx", 1.0), ("en", 1.0), ("en", 1.0)]))
    assert language.languages_of("t", LONG) == ["en"]


def test_a_window_counts_only_when_it_is_sure():
    _with(_Fake([("en", 0.99)], windows=[("en", 1.0), ("es", language.WINDOW_SHARE - 0.01), ("en", 1.0), ("en", 1.0)]))
    assert language.languages_of("t", LONG) == ["en"]
    _with(_Fake([("en", 0.99)], windows=[("en", 1.0), ("es", language.WINDOW_SHARE), ("en", 1.0), ("en", 1.0)]))
    assert language.languages_of("t", LONG) == ["en", "es"]


def test_the_windows_are_spread_over_the_whole_text():
    # No two windows of it alike, so a window read from the wrong place shows.
    text = " ".join(str(i) for i in range(language.MAX_CHARS))
    fake = _with(_Fake([("en", 0.99)]))
    language.languages_of(None, text)
    full = language.text_of(None, text)
    assert len(fake.asked) == language.WINDOWS
    assert fake.asked[0] == full[: language.WINDOW_CHARS]
    assert fake.asked[-1] == full[-language.WINDOW_CHARS :]


def test_the_most_told_other_language_is_the_second():
    _with(_Fake([("en", 0.99)], windows=[("de", 1.0), ("es", 1.0), ("es", 1.0), ("en", 1.0)]))
    assert language.languages_of("t", LONG) == ["en", "es"]


def test_a_short_text_reads_no_windows():
    fake = _with(_Fake([("en", 0.99)]))
    assert language.languages_of("t", "y" * language.WINDOW_CHARS) == ["en"]
    assert fake.asked == []


# ---- the database step


def _conn(missing=()):
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = list(missing)
    return conn


def test_no_post_ids_reads_nothing():
    conn = _conn()
    assert language.detect_post_languages(conn, []) == 0
    conn.cursor.assert_not_called()


def test_the_posts_without_languages_are_detected_stored_and_committed(monkeypatch):
    written = {}
    monkeypatch.setattr(language, "languages_of", lambda title, body: [title])
    def _execute_values(cur, sql, rows, template, fetch):
        written.update(sql=sql, rows=rows, template=template, fetch=fetch)
        return [(1,)]  # post 2 was pruned meanwhile: only 1 stored

    monkeypatch.setattr(language.psycopg2.extras, "execute_values", _execute_values)
    conn = _conn(missing=[(1, "es", "cuerpo"), (2, "en", "body")])
    assert language.detect_post_languages(conn, [1, 2, 3]) == 1
    cur = conn.cursor.return_value.__enter__.return_value
    assert cur.execute.call_args.args == (
        language.MISSING_SQL, {"post_ids": [1, 2, 3], "chars": 2 * language.MAX_CHARS})
    assert written == {"sql": language.INSERT_SQL, "rows": [(1, ["es"]), (2, ["en"])],
                       "template": "(%s::int, %s::text[])", "fetch": True}
    # Once after the read, so the detector does not run inside a transaction, and once
    # for the insert.
    assert conn.commit.call_count == 2


def test_nothing_missing_writes_nothing(monkeypatch):
    monkeypatch.setattr(language.psycopg2.extras, "execute_values", lambda *a, **k: pytest.fail("wrote"))
    conn = _conn(missing=[])
    assert language.detect_post_languages(conn, [1]) == 0
    conn.commit.assert_called_once()  # the read's


def test_a_database_error_is_rolled_back_and_logged(caplog):
    conn = _conn()
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = psycopg2.errors.UndefinedTable("no table")
    with caplog.at_level(logging.WARNING, logger="haf_fyp.language"):
        assert language.detect_post_languages(conn, [1, 2]) == 0
    conn.rollback.assert_called_once()
    assert any("language detection failed for 2 posts" in r.getMessage() for r in caplog.records)


def test_a_detector_failure_is_rolled_back_and_logged_and_the_cycle_ranks_on(monkeypatch, caplog):
    def _broken(title, body):
        raise RuntimeError("model would not load")

    monkeypatch.setattr(language, "languages_of", _broken)
    conn = _conn(missing=[(1, "t", "b")])
    with caplog.at_level(logging.WARNING, logger="haf_fyp.language"):
        assert language.detect_post_languages(conn, [1]) == 0
    conn.rollback.assert_called_once()
    assert any("language detection failed for 1 posts" in r.getMessage() for r in caplog.records)


def test_a_lost_connection_is_raised():
    conn = _conn()
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = psycopg2.OperationalError("gone")
    with pytest.raises(psycopg2.OperationalError):
        language.detect_post_languages(conn, [1])
    conn.rollback.assert_not_called()
