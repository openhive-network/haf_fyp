"""Connection settings from the environment, with the shell's semantics.

scripts/common.sh writes `: "${POSTGRES_HOST:=haf}"`. The COLON form substitutes when
the variable is unset OR set-but-empty, so an empty POSTGRES_HOST becomes `haf` there.

os.environ.get(key, default) does not do that -- it returns the default only when the
key is absent. Every service in this package used it, so `POSTGRES_HOST=` in a compose
file or env_file produced `host=` and libpq substituted ITS default, the local socket.
That is the same failure as the empty `--host=` flag (#40), reached through the channel
the flag guard cannot see.

Whitespace-only is rejected rather than defaulted. ranker_daemon builds its DSN by
f-string concatenation, so a value containing a space injects conninfo keywords --
POSTGRES_HOST="x dbname=other" would silently change the database.
"""

from __future__ import annotations

import os


class ConnectionSettingError(RuntimeError):
    """A connection variable is set to something that cannot be meant."""


def env_or_default(key: str, default: str) -> str:
    """The value of `key`, treating unset and empty alike -- as the shell's `:=` does.

    Raises rather than defaulting when the value is whitespace-only or contains a
    character that would break out of a conninfo field. Defaulting there would hide a
    misconfiguration; failing names it.
    """
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    if not raw.strip():
        raise ConnectionSettingError(
            f"{key} is set to a whitespace-only value ({raw!r}). Refused: it cannot be "
            f"what was meant, and it would be concatenated into a libpq conninfo."
        )
    if any(c.isspace() for c in raw) or "'" in raw or "\\" in raw:
        raise ConnectionSettingError(
            f"{key}={raw!r} contains whitespace or a quote. Refused: these values are "
            f"concatenated into a libpq conninfo, where a space starts a new keyword -- "
            f'POSTGRES_HOST="x dbname=other" would silently change the database.'
        )
    return raw
