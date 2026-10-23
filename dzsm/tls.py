"""A certificate dzsm makes for itself, so the dashboard can be reached over HTTPS.

The whole access model rests on one token. Over plain HTTP that token crosses the LAN
in a header on every single request, readable by anything that can capture the
traffic — and whoever holds it can stop servers, delete mods, ban players and restore
an old world over a live one. TLS is what keeps it the user's.

Self-signed, because there is no public name to get a real certificate for. That makes
the browser warn the first time, and a warning that gets clicked through blindly
protects nothing — so the fingerprint is printed at startup and shown on the dashboard,
and the first connection from a new PC is a comparison, not a leap of faith. A matching
fingerprint is the thing that defeats someone answering in the server's place.

The certificate is reused for as long as it is valid and still names the address being
served. Every new one is a new fingerprint and a new warning on every PC that has
connected before, so it must not churn.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from dzsm.config import Config

_log = logging.getLogger(__name__)

CERT_NAME = "dzsm.crt"
KEY_NAME = "dzsm.key"

# 825 days is the longest validity any browser has accepted for a certificate it was
# not told to trust specially. Long enough that it does not expire under a server that
# runs for a year; short enough that nothing refuses it on sight.
VALIDITY_DAYS = 825

# Renewed this far ahead of expiry rather than on the day, so the new fingerprint
# arrives while there is still time to compare it calmly.
RENEW_WITHIN_DAYS = 30


class TlsError(Exception):
    """The certificate could not be made, read or used."""


@dataclass(frozen=True)
class TlsMaterial:
    """What uvicorn needs, plus what a person needs to check they reached the right box."""

    cert_path: Path
    key_path: Path
    fingerprint: str
    """SHA-256 of the certificate, as browsers show it: AB:CD:..."""
    generated: bool
    """True when this call made a new one — worth saying, since every PC will warn."""


def tls_dir(config: Config) -> Path:
    return config.root / "tls"


def ensure_certificate(config: Config, bind_host: str) -> TlsMaterial:
    """The certificate to serve with, made if it is missing or no longer fits.

    Bring-your-own wins: `api.cert_file` and `api.key_file` are used exactly as given,
    never rewritten — a certificate from a home CA removes the browser warning entirely,
    and replacing it would be vandalism.

    Raises TlsError when a supplied certificate cannot be read, or one cannot be made.
    """
    api = config.api
    if api.cert_file is not None or api.key_file is not None:
        if api.cert_file is None or api.key_file is None:
            raise TlsError("api.cert_file and api.key_file must be set together")
        for path in (api.cert_file, api.key_file):
            if not path.is_file():
                raise TlsError(f"{path} does not exist")
        cert = _load(api.cert_file)
        return TlsMaterial(api.cert_file, api.key_file, fingerprint(cert), generated=False)

    directory = tls_dir(config)
    cert_path, key_path = directory / CERT_NAME, directory / KEY_NAME
    names = _names_for(bind_host)

    if cert_path.is_file() and key_path.is_file():
        try:
            cert = _load(cert_path)
        except TlsError as exc:
            _log.warning("existing certificate unreadable, replacing it: %s", exc)
        else:
            reason = _why_replace(cert, names)
            if reason is None:
                return TlsMaterial(cert_path, key_path, fingerprint(cert), generated=False)
            _log.warning("replacing the dashboard certificate: %s", reason)

    cert = _generate(cert_path, key_path, names)
    return TlsMaterial(cert_path, key_path, fingerprint(cert), generated=True)


def fingerprint(cert: x509.Certificate) -> str:
    """SHA-256 in the form browser certificate viewers show, so it can be read across."""
    digest = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{byte:02X}" for byte in digest)


def read_fingerprint(path: Path) -> str | None:
    """The fingerprint of the certificate at `path`, or None when it cannot be read."""
    try:
        return fingerprint(_load(path))
    except TlsError:
        return None


def certificate_names(cert: x509.Certificate) -> set[str]:
    """Every DNS name and IP address the certificate is valid for."""
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    names = {str(name) for name in san.get_values_for_type(x509.DNSName)}
    names |= {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    return names


# --- internals -----------------------------------------------------------------


def _names_for(bind_host: str) -> set[str]:
    """Everything someone on the LAN might type to reach this machine.

    The bind address itself, every local IPv4 address — a server with two network
    cards is reached by whichever one the other PC is on — the hostname, and the
    loopback names so the server's own browser works too.

    0.0.0.0 is not a name anyone types, so it is replaced by the addresses it stands
    for rather than written into the certificate.
    """
    names = {"localhost", "127.0.0.1"}
    if bind_host not in {"0.0.0.0", "::", ""}:  # noqa: S104 - compared, not bound
        names.add(bind_host)
    hostname = socket.gethostname()
    if hostname:
        names.add(hostname.lower())
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            names.add(str(info[4][0]))
    except OSError as exc:
        _log.debug("could not list local addresses: %s", exc)
    return names


def _why_replace(
    cert: x509.Certificate, names: set[str], *, now: dt.datetime | None = None
) -> str | None:
    """Why the existing certificate will not do, or None when it will."""
    now = now or dt.datetime.now(dt.UTC)
    if cert.not_valid_after_utc - now < dt.timedelta(days=RENEW_WITHIN_DAYS):
        return f"it expires {cert.not_valid_after_utc:%Y-%m-%d}"
    missing = names - certificate_names(cert)
    if missing:
        # Only the bind address and the stable names really matter, but any missing
        # address means some PC on the LAN would get a name-mismatch error.
        return f"it does not cover {', '.join(sorted(missing))}"
    return None


def _generate(cert_path: Path, key_path: Path, names: set[str]) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dzsm dashboard")])

    alt: list[x509.GeneralName] = []
    for name in sorted(names):
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt.append(x509.DNSName(name))

    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # A minute in the past, so a client whose clock runs slightly behind does not
        # reject a certificate that is "not valid yet".
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    try:
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        # Key first, then the certificate: a certificate on disk with no matching key
        # is the state that would be "reused" and then fail at the TLS handshake.
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    except OSError as exc:
        raise TlsError(f"cannot write the certificate to {cert_path.parent}: {exc}") from exc

    _log.info(
        "made a new dashboard certificate for %s (fingerprint %s)",
        ", ".join(sorted(names)),
        fingerprint(cert),
    )
    return cert


def _load(path: Path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise TlsError(f"cannot read the certificate at {path}: {exc}") from exc
