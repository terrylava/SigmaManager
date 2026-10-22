"""Telling someone, without them watching the dashboard.

Everything this announces happens when nobody is looking: a crash loop at 3am, a mod
that cannot be fetched, a game update that will start refusing clients. The module's
whole job is to be the thing that still works when the rest is on fire, so what is
tested is mostly that it never makes anything worse.
"""

from __future__ import annotations

import httpx
import pytest

from dzsm import notify
from dzsm.config import Config, ConfigError, NotifyConfig, load_config

WEBHOOK = "https://example.invalid/hook"
ENV = "DZSM_TEST_WEBHOOK"


@pytest.fixture
def cfg(config: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv(ENV, WEBHOOK)
    return config.model_copy(update={"notify": NotifyConfig(webhook_url_env=ENV)})


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        calls.append({"url": url, **kwargs})
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr("dzsm.notify.httpx.post", fake_post)
    return calls


# --- sending -------------------------------------------------------------------


def test_an_event_is_delivered(cfg: Config, sent: list[dict[str, object]]) -> None:
    assert notify.post(cfg, "crash", "chernarus stopped") is True
    assert sent[0]["url"] == WEBHOOK
    assert sent[0]["json"] == {"content": "chernarus stopped"}


def test_nothing_is_sent_without_a_webhook(
    config: Config, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, object]]
) -> None:
    """The common case: most people will never set one, and it must cost nothing."""
    monkeypatch.delenv(ENV, raising=False)
    quiet = config.model_copy(update={"notify": NotifyConfig(webhook_url_env=ENV)})

    assert notify.post(quiet, "crash", "x") is False
    assert sent == []


def test_a_disabled_event_is_not_sent(cfg: Config, sent: list[dict[str, object]]) -> None:
    picky = cfg.model_copy(
        update={"notify": NotifyConfig(webhook_url_env=ENV, events=["gave_up"])}
    )

    assert notify.post(picky, "crash", "x") is False
    assert notify.post(picky, "gave_up", "x") is True


def test_an_unknown_event_is_refused_loudly(
    cfg: Config, sent: list[dict[str, object]], caplog: pytest.LogCaptureFixture
) -> None:
    """A typo in a call site would otherwise be silence, which is the one thing this
    module exists to prevent."""
    assert notify.post(cfg, "explosion", "x") is False
    assert "unknown event" in caplog.text
    assert sent == []


def test_a_long_message_is_truncated_not_dropped(
    cfg: Config, sent: list[dict[str, object]]
) -> None:
    """Discord rejects anything past 2000 characters. One that arrives cut short still
    tells someone to go and look."""
    notify.post(cfg, "crash", "x" * 5000)

    body = sent[0]["json"]
    assert isinstance(body, dict)
    assert len(str(body["content"])) == notify.MAX_LENGTH


# --- never making things worse ---------------------------------------------------


def test_a_webhook_that_is_down_is_not_an_error(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is called from the supervision loop. A webhook that has gone away must not
    be able to interfere with crash detection (Rule 3: say so, do not fail)."""

    def refuse(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("dzsm.notify.httpx.post", refuse)

    assert notify.post(cfg, "crash", "x") is False


def test_a_rejected_post_is_not_an_error(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rejected(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("POST", url))

    monkeypatch.setattr("dzsm.notify.httpx.post", rejected)

    assert notify.post(cfg, "crash", "x") is False


def test_the_url_never_reaches_the_log(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It is a credential — anyone holding it can post as the server — and a log line
    carrying one outlives the incident it was written for."""

    def refuse(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("dzsm.notify.httpx.post", refuse)
    notify.post(cfg, "crash", "x")

    assert WEBHOOK not in caplog.text


# --- config ----------------------------------------------------------------------


def test_an_unknown_event_in_config_is_refused(tmp_path: object) -> None:
    """Rather than silently never firing, which looks exactly like "nothing happened"."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    path = tmp_path / "config.toml"
    path.write_text(
        f"root = '{tmp_path.as_posix()}'\n"
        f"allow_long_root = true\n"
        f"[steam]\nusername = 'x'\nsteamcmd_path = '{tmp_path.as_posix()}/s.exe'\n"
        f"[notify]\nevents = ['crash', 'explosion']\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown notify event"):
        load_config(path)
