"""POSTGRES_URL must win for the Python services too (#31).

The shell scripts honour it via scripts/common.sh. If the API and the ranker did not,
setting it on the stack would point install/process/uninstall at one database while these
two connected to another -- the same split-brain the flag was backed out for once, except
silent: feeds would simply stop matching the chain, with nothing in any log to say why.

These assert the CONTRACT (the URL replaces the discrete settings, and is passed as a dsn
rather than merged) without opening a connection.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("POSTGRES_URL", "POSTGRES_HOST", "POSTGRES_PORT",
              "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _api():
    return importlib.import_module("haf_fyp.api")


def _ranker():
    return importlib.import_module("haf_fyp.ranker_daemon")


# --- the API pool ------------------------------------------------------------------

def test_api_uses_the_url_as_a_dsn_when_set(clean_env):
    clean_env.setenv("POSTGRES_URL", "postgresql://someone@elsewhere:6543/otherdb")
    clean_env.setenv("POSTGRES_HOST", "haf")
    kwargs = _api()._db_kwargs()
    assert kwargs == {"dsn": "postgresql://someone@elsewhere:6543/otherdb"}, (
        "the URL must REPLACE the discrete settings, not be merged with them -- a "
        "merged connection goes somewhere the logs do not name"
    )
    assert "host" not in kwargs


def test_api_falls_back_to_the_discrete_settings(clean_env):
    clean_env.setenv("POSTGRES_HOST", "haf")
    clean_env.setenv("POSTGRES_USER", "haf_fyp_user")
    kwargs = _api()._db_kwargs()
    assert kwargs["host"] == "haf"
    assert kwargs["user"] == "haf_fyp_user"
    assert "dsn" not in kwargs


def test_api_treats_an_empty_url_as_unset(clean_env):
    """An empty expansion must not become dsn="" -- psycopg2 would then connect to the
    libpq defaults, which is neither what the env says nor what the operator meant."""
    clean_env.setenv("POSTGRES_URL", "   ")
    clean_env.setenv("POSTGRES_HOST", "haf")
    kwargs = _api()._db_kwargs()
    assert "dsn" not in kwargs
    assert kwargs["host"] == "haf"


# --- the ranker --------------------------------------------------------------------

def test_ranker_uses_the_url_verbatim_when_set(clean_env):
    clean_env.setenv("POSTGRES_URL", "postgresql://someone@elsewhere:6543/otherdb")
    clean_env.setenv("POSTGRES_HOST", "haf")
    assert _ranker()._dsn() == "postgresql://someone@elsewhere:6543/otherdb"


def test_ranker_falls_back_to_the_discrete_settings(clean_env):
    clean_env.setenv("POSTGRES_HOST", "haf")
    dsn = _ranker()._dsn()
    assert "host=haf" in dsn
    assert not dsn.startswith("postgresql://")


def test_ranker_treats_an_empty_url_as_unset(clean_env):
    clean_env.setenv("POSTGRES_URL", "")
    clean_env.setenv("POSTGRES_HOST", "haf")
    assert "host=haf" in _ranker()._dsn()


# --- and the two must not diverge ---------------------------------------------------

def test_both_services_agree_about_which_source_wins(clean_env):
    """The failure this exists to prevent is one service honouring the URL and the other
    not: the ranker would write to one database while the API read another, and nothing
    would report it."""
    clean_env.setenv("POSTGRES_URL", "postgresql://u@h/db")
    clean_env.setenv("POSTGRES_HOST", "haf")
    assert _api()._db_kwargs() == {"dsn": "postgresql://u@h/db"}
    assert _ranker()._dsn() == "postgresql://u@h/db"
