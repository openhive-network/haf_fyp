"""
The languages a post is written in (#115), for the feeds' `language` parameter.

Hive posts rarely say: on testapi (2026-11-03) 7 of 5,000 candidates carried a language in
their json_metadata, and a community's `lang` was 'en' for 90% of posts, whatever they
were written in. So the ranker detects it from the post's own title and body with
py3langid (langid.py's model, shipped inside the package: no download, no network), once
per post. About 2 ms a post on one core (2,000 testapi candidates in 4.2 s); a cycle
detects only the candidates it has not seen before.

A post keeps up to two languages, so one written in both Spanish and English is in both
languages' feeds. Over a whole post the detector leans hard to one language (two sentences,
one English and one Spanish, came out 0.64 Spanish and 0.18 English), and Hive's Spanish-speaking authors often
write the post twice, "[ENG|ESP]". So the first language is the whole text's, if it has at
least MIN_SHARE of the probability; the second is the whole text's runner-up at MIN_SHARE,
or failing that whichever other language a window of the text is told in with at least
WINDOW_SHARE (WINDOWS of them, spread over the post). Over those 2,000 candidates 239 posts
got two, 196 of them Spanish and English; with the whole text alone, 23. None at all (31
of them: a caption of a few words, a video link) leaves the list empty, and the post in no
language's feed.

Stored in haf_fyp_app.post_language, keyed by the hivemind post id and pruned with
post_candidates, like post_bridge_cache. A post is not detected again when it is edited.

Best effort: a failure is logged and the cycle ranks on. Posts it missed are detected on
the next cycle, and are left out of language-filtered feeds until then. Lives outside
ranker_daemon because .coveragerc omits that module.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

import psycopg2
import psycopg2.extras
from py3langid.langid import MODEL_FILE, LanguageIdentifier  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

# Over the 2,000 testapi candidates, what fell below 0.3 was captions and links.
MIN_SHARE = 0.3
MAX_LANGUAGES = 2
# Long enough to reach the second version of a post written twice, and a bound on the
# work per post. The body is read at twice this, as markup and links come out of it
# before the cut.
MAX_CHARS = 6000
WINDOWS = 4
WINDOW_CHARS = 600
WINDOW_SHARE = 0.9

# Labels the model has for text that is no language.
_NOT_A_LANGUAGE = frozenset({"zxx", "und"})

_MARKUP = (
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),  # images
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),  # links: keep their text
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"<[^>]+>"), " "),
    (re.compile(r"[@#]\w[\w.-]*"), " "),  # accounts and hashtags
)
_SPACE = re.compile(r"\s+")

_identifier: LanguageIdentifier | None = None

MISSING_SQL = """
    SELECT pd.id, pd.title, left(pd.body, %(chars)s)
    FROM unnest(%(post_ids)s::int[]) AS c(post_id)
    JOIN hivemind_app.hive_post_data AS pd ON pd.id = c.post_id
    WHERE NOT EXISTS (SELECT 1 FROM haf_fyp_app.post_language AS pl
                      WHERE pl.post_id = c.post_id)
"""

# Only posts still in the window: one the retention pass deleted since the candidates were
# read would fail the foreign key, and with it the whole batch.
INSERT_SQL = """
    INSERT INTO haf_fyp_app.post_language (post_id, languages)
    SELECT v.post_id, v.languages
    FROM (VALUES %s) AS v(post_id, languages)
    WHERE EXISTS (SELECT 1 FROM haf_fyp_app.post_candidates AS pc WHERE pc.post_id = v.post_id)
    ON CONFLICT (post_id) DO NOTHING
"""


def text_of(title: str | None, body: str | None) -> str:
    """What the detector reads: the title and body without markup, links, accounts and
    hashtags, which are no language and would otherwise pull every post towards English."""
    text = f"{title or ''}. {body or ''}"
    for pattern, replacement in _MARKUP:
        text = pattern.sub(replacement, text)
    return _SPACE.sub(" ", text).strip()[:MAX_CHARS]


def _is_language(lang: str, share: float, at_least: float) -> bool:
    return share >= at_least and lang not in _NOT_A_LANGUAGE


def languages_of(title: str | None, body: str | None) -> list[str]:
    """The post's languages, most likely first: ISO 639-1 codes, at most MAX_LANGUAGES."""
    global _identifier
    text = text_of(title, body)
    if not text.strip(". "):
        return []
    if _identifier is None:
        _identifier = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
    found = [lang for lang, share in _identifier.rank(text)[:MAX_LANGUAGES] if _is_language(lang, share, MIN_SHARE)]
    if len(found) == 1 and len(text) > 2 * WINDOW_CHARS:
        step = (len(text) - WINDOW_CHARS) / (WINDOWS - 1)
        others: Counter[str] = Counter()
        for i in range(WINDOWS):
            start = int(i * step)
            lang, share = _identifier.classify(text[start : start + WINDOW_CHARS])
            if lang != found[0] and _is_language(lang, share, WINDOW_SHARE):
                others[lang] += 1
        if others:
            found.append(others.most_common(1)[0][0])
    return found


def detect_post_languages(conn, post_ids: list[int]) -> int:
    """Detect and store the languages of the posts among `post_ids` that have none
    stored yet. Returns how many were detected. A lost connection is raised for the main
    loop to reconnect; any other database error is logged and rolled back."""
    if not post_ids:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(MISSING_SQL, {"post_ids": list(post_ids), "chars": 2 * MAX_CHARS})
            rows = cur.fetchall()
        if not rows:
            return 0
        found = [(post_id, languages_of(title, body)) for post_id, title, body in rows]
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, INSERT_SQL, found, template="(%s::int, %s::text[])")
        conn.commit()
    except psycopg2.OperationalError:
        raise
    except psycopg2.Error:
        conn.rollback()
        log.warning(
            "language detection failed for %d posts (continuing; the next cycle retries)",
            len(post_ids),
            exc_info=True,
        )
        return 0
    return len(found)
