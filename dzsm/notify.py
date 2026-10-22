"""Telling someone when something happened, without them watching the dashboard.

A crash loop, a server that gave up, a mod that could not be fetched, a game update —
all of it currently waits to be noticed. The person running the server is usually
somewhere else.

One webhook, Discord-shaped, because that is where DayZ server admins already are and
every generic receiver accepts the same `{"content": ...}` body. The URL is a secret —
anyone holding it can post as the server — so it comes from an environment variable
named in config, never from the config file, and it is never logged. Same rule as the
Steam password.
"""

from __future__ import annotations

import logging
import os

import httpx

from dzsm.config import Config

_log = logging.getLogger(__name__)

# What can be announced. A name here is what config enables, so renaming one is a
# breaking change to somebody's config rather than a tidy-up.
EVENTS: tuple[str, ...] = (
    "crash",  # an instance died and is being restarted
    "gave_up",  # it failed too many times and the supervisor stopped
    "rollback",  # it went back to the mod versions it last stayed up on
    "mod_fetch_failed",  # a mod an instance needs could not be downloaded
    "game_update",  # Steam has a newer DayZ server build
    "dzsm_update",  # a newer dzsm has been published
    "backup_failed",  # the world could not be copied
)

# Discord rejects anything longer. Truncated rather than dropped: a message that arrives
# cut short still tells someone to go and look.
MAX_LENGTH = 2000


def post(config: Config, event: str, text: str) -> bool:
    """Announce one event. True if it was sent.

    Never raises and never retries. This is called from the supervision loop and from
    the housekeeping tick, and a webhook that is down must not be able to interfere with
    either (Rule 3: say so, do not fail).
    """
    if event not in EVENTS:
        # A typo in a call site would otherwise be silence, which is the one thing this
        # module exists to prevent.
        _log.error("notify: unknown event %r, nothing sent", event)
        return False
    if event not in config.notify.events:
        return False

    url = webhook_url(config)
    if not url:
        return False

    body = text if len(text) <= MAX_LENGTH else text[: MAX_LENGTH - 1] + "…"
    try:
        response = httpx.post(
            url,
            json={"content": body},
            timeout=config.notify.timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # Deliberately without the URL: it is a credential, and a log line carrying one
        # outlives the incident it was written for.
        _log.warning("notify: could not deliver %s: %s", event, type(exc).__name__)
        return False
    return True


def webhook_url(config: Config) -> str | None:
    """The configured webhook, or None when there is none.

    From the environment only. A URL in config.toml is a credential in a file people
    paste into forum posts when asking for help.
    """
    return os.environ.get(config.notify.webhook_url_env) or None
