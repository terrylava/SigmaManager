"""HTTPS for reaching the dashboard from another PC.

Over plain HTTP the API token crosses the network in clear text on every request, and
whoever reads it can stop servers, delete mods and restore an old world over a live
one. The certificate exists to keep that token the user's.

The part that is easy to get wrong is churn. Every new certificate is a new fingerprint
and a fresh browser warning on every PC that has ever connected, which teaches people to
click through warnings — the opposite of what a self-signed certificate needs them to
do. So most of what is tested is when it is *not* regenerated.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dzsm import tls
from dzsm.cli import _check_bind, app
from dzsm.config import ApiConfig, Config


@pytest.fixture
def cfg(config: Config) -> Config:
    return config.model_copy(update={"api": ApiConfig(tls=True)})


def _cert(material: tls.TlsMaterial) -> object:
    from cryptography import x509

    return x509.load_pem_x509_certificate(material.cert_path.read_bytes())


# --- making one ------------------------------------------------------------------


def test_a_certificate_is_made_when_there_is_none(cfg: Config) -> None:
    material = tls.ensure_certificate(cfg, "192.168.1.150")

    assert material.generated is True
    assert material.cert_path.is_file()
    assert material.key_path.is_file()


def test_it_covers_what_someone_would_type(cfg: Config) -> None:
    """The bind address, and the loopback names so the server's own browser works."""
    material = tls.ensure_certificate(cfg, "192.168.1.150")
    names = tls.certificate_names(_cert(material))  # type: ignore[arg-type]

    assert {"192.168.1.150", "localhost", "127.0.0.1"} <= names


def test_binding_everywhere_names_real_addresses_not_zero(cfg: Config) -> None:
    """0.0.0.0 is not something anyone types into a browser."""
    material = tls.ensure_certificate(cfg, "0.0.0.0")  # noqa: S104 - a test value
    names = tls.certificate_names(_cert(material))  # type: ignore[arg-type]

    assert "0.0.0.0" not in names  # noqa: S104
    assert "localhost" in names


def test_the_fingerprint_reads_the_way_browsers_show_it(cfg: Config) -> None:
    """So it can be compared character for character with the certificate viewer."""
    fingerprint = tls.ensure_certificate(cfg, "127.0.0.1").fingerprint
    parts = fingerprint.split(":")

    assert len(parts) == 32
    assert all(len(part) == 2 and part == part.upper() for part in parts)


# --- not churning -------------------------------------------------------------------


def test_a_second_start_reuses_it(cfg: Config) -> None:
    """The important one. A new certificate every start would mean a new warning on
    every PC every time, and people learn to click through warnings they see daily."""
    first = tls.ensure_certificate(cfg, "192.168.1.150")
    second = tls.ensure_certificate(cfg, "192.168.1.150")

    assert second.generated is False
    assert second.fingerprint == first.fingerprint


def test_a_new_address_makes_a_new_one(cfg: Config) -> None:
    """Otherwise the browser rejects it for a name mismatch, which is not the warning
    people should be learning to accept."""
    first = tls.ensure_certificate(cfg, "192.168.1.150")
    second = tls.ensure_certificate(cfg, "10.0.0.5")

    assert second.generated is True
    assert second.fingerprint != first.fingerprint


def test_one_nearly_expired_is_renewed(cfg: Config) -> None:
    """Ahead of the day, so the new fingerprint arrives while there is time to check it."""
    material = tls.ensure_certificate(cfg, "127.0.0.1")
    cert = _cert(material)
    names = tls.certificate_names(cert)  # type: ignore[arg-type]
    soon = dt.datetime.now(dt.UTC) + dt.timedelta(days=tls.VALIDITY_DAYS - 10)

    assert tls._why_replace(cert, names) is None  # type: ignore[arg-type]
    assert "expires" in (tls._why_replace(cert, names, now=soon) or "")  # type: ignore[arg-type]


def test_an_unreadable_one_is_replaced_not_fatal(cfg: Config) -> None:
    first = tls.ensure_certificate(cfg, "127.0.0.1")
    first.cert_path.write_text("not a certificate", encoding="utf-8")

    assert tls.ensure_certificate(cfg, "127.0.0.1").generated is True


# --- bring your own ---------------------------------------------------------------------


def test_a_supplied_certificate_is_used_untouched(cfg: Config, tmp_path: Path) -> None:
    """One from a home CA removes the browser warning entirely, and rewriting it would
    be vandalism."""
    made = tls.ensure_certificate(cfg, "127.0.0.1")
    mine_cert, mine_key = tmp_path / "mine.crt", tmp_path / "mine.key"
    mine_cert.write_bytes(made.cert_path.read_bytes())
    mine_key.write_bytes(made.key_path.read_bytes())
    before = mine_cert.read_bytes()
    own = cfg.model_copy(
        update={"api": ApiConfig(tls=True, cert_file=mine_cert, key_file=mine_key)}
    )

    # A different address would make dzsm regenerate its own; it must not touch these.
    material = tls.ensure_certificate(own, "10.9.9.9")

    assert material.cert_path == mine_cert
    assert material.generated is False
    assert mine_cert.read_bytes() == before


def test_half_a_supplied_pair_is_refused(cfg: Config, tmp_path: Path) -> None:
    lonely = tmp_path / "only.crt"
    lonely.write_text("x", encoding="utf-8")
    half = cfg.model_copy(update={"api": ApiConfig(tls=True, cert_file=lonely)})

    with pytest.raises(tls.TlsError, match="set together"):
        tls.ensure_certificate(half, "127.0.0.1")


# --- the bind rule -------------------------------------------------------------------------


def test_loopback_needs_neither_token_nor_tls(config: Config) -> None:
    """The default setup must keep working exactly as before."""
    _check_bind(config, "127.0.0.1")


def test_the_lan_without_tls_is_refused(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token in clear text on the network is barely better than no token."""
    monkeypatch.setenv("DZSM_API_TOKEN", "a-real-token")
    runner = CliRunner()

    @app.command("bindprobe", hidden=True)
    def _probe() -> None:
        _check_bind(config, "192.168.1.150")

    result = runner.invoke(app, ["bindprobe"])

    assert result.exit_code == 1
    assert "plain HTTP" in result.output


def test_the_lan_with_tls_and_a_token_is_allowed(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DZSM_API_TOKEN", "a-real-token")

    _check_bind(cfg, "192.168.1.150")


def test_plain_http_can_be_chosen_deliberately(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For a network nobody else can see into — chosen, never defaulted into."""
    monkeypatch.setenv("DZSM_API_TOKEN", "a-real-token")
    plain = config.model_copy(update={"api": ApiConfig(allow_plain_http=True)})

    _check_bind(plain, "192.168.1.150")


def test_tls_does_not_replace_the_token(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Encryption keeps the token secret; it is not a substitute for having one."""
    monkeypatch.delenv("DZSM_API_TOKEN", raising=False)
    runner = CliRunner()

    @app.command("bindprobe2", hidden=True)
    def _probe() -> None:
        _check_bind(cfg, "192.168.1.150")

    result = runner.invoke(app, ["bindprobe2"])

    assert result.exit_code == 1
    assert "without a token" in result.output
