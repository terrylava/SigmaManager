"""BattlEye RCON (spec: restart warnings, admin chat).

DayZ's RCON is BattlEye's, not Source's: UDP, CRC32-framed, sequence-numbered. The
framing is the part that fails silently — a wrong CRC gets no reply at all rather than an
error — so it is built and parsed explicitly here and unit tested against real bytes.

Packet layout:

    'B' 'E' <crc32 of payload, little endian> <payload>

and every payload starts with 0xFF followed by a type byte:

    0x00  login          data = password;         reply data = 0x01 ok / 0x00 denied
    0x01  command        data = seq + command;    reply data = seq + response
    0x02  server message data = seq + text;       must be acknowledged with 0x02 + seq

A connection that sends nothing for 45s is dropped, so anything holding one open needs
to send at least an empty command periodically.
"""

from __future__ import annotations

import logging
import socket
import zlib
from types import TracebackType

_log = logging.getLogger(__name__)

HEADER = b"BE"
PREFIX = 0xFF
LOGIN = 0x00
COMMAND = 0x01
SERVER_MESSAGE = 0x02

LOGIN_OK = 0x01
# BattlEye drops a connection idle for 45s; keep well inside that.
KEEPALIVE_SECONDS = 25.0


class RconError(Exception):
    """RCON is unreachable, refused the password, or answered nonsense."""


def build_packet(payload: bytes) -> bytes:
    """Frame a payload. The CRC covers the payload only, and is little endian."""
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    return HEADER + checksum.to_bytes(4, "little") + payload


def parse_packet(packet: bytes) -> tuple[int, bytes]:
    """Return (type, data) from a framed packet.

    Raises RconError on anything malformed — a corrupted reply must not be mistaken for
    a successful command.
    """
    if len(packet) < 8 or not packet.startswith(HEADER):
        raise RconError(f"not a BattlEye packet: {packet[:16]!r}")

    expected = int.from_bytes(packet[2:6], "little")
    payload = packet[6:]
    actual = zlib.crc32(payload) & 0xFFFFFFFF
    if expected != actual:
        raise RconError(f"CRC mismatch: header says {expected:#010x}, payload is {actual:#010x}")

    if payload[0] != PREFIX:
        raise RconError(f"payload does not start with 0xFF: {payload[:4]!r}")
    return payload[1], payload[2:]


class RconClient:
    """One RCON connection. Use as a context manager.

    Deliberately synchronous and short-lived by default: the caller decides how long to
    hold it. Nothing here retries — a caller sending restart warnings must not have a
    flaky socket delay the restart itself.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self._password = password
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._sequence = 0

    def __enter__(self) -> RconClient:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> None:
        """Open the socket and authenticate. Raises RconError if the password is wrong."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect((self.host, self.port))
            sock.send(build_packet(bytes([PREFIX, LOGIN]) + self._password.encode("utf-8")))
            packet_type, data = parse_packet(self._receive(sock))
        except (OSError, RconError) as exc:
            sock.close()
            raise RconError(f"RCON connect to {self.host}:{self.port} failed: {exc}") from exc

        if packet_type != LOGIN or not data or data[0] != LOGIN_OK:
            sock.close()
            raise RconError(
                f"RCON login refused by {self.host}:{self.port} — check the password in "
                f"the server's BEServer config"
            )

        self._socket = sock
        _log.debug("rcon: connected to %s:%s", self.host, self.port)

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def command(self, command: str) -> str:
        """Run a command and return its response text.

        Server-message packets arriving mid-exchange are acknowledged and skipped: BE
        interleaves them with replies, and failing to ack makes the server retransmit.
        """
        sock = self._require_socket()
        sequence = self._sequence & 0xFF
        self._sequence += 1

        payload = bytes([PREFIX, COMMAND, sequence]) + command.encode("utf-8")
        try:
            sock.send(build_packet(payload))
            for _ in range(8):
                packet_type, data = parse_packet(self._receive(sock))
                if packet_type == SERVER_MESSAGE and data:
                    sock.send(build_packet(bytes([PREFIX, SERVER_MESSAGE, data[0]])))
                    continue
                if packet_type == COMMAND and data and data[0] == sequence:
                    return _command_text(data[1:])
        except (OSError, RconError) as exc:
            raise RconError(f"RCON command {command!r} failed: {exc}") from exc

        raise RconError(f"RCON command {command!r} got no matching reply")

    def keepalive(self) -> None:
        """Empty command, purely to stop BattlEye dropping an idle connection."""
        self.command("")

    def say(self, message: str) -> None:
        """Broadcast to every player. `say -1` is BattlEye's global admin chat."""
        self.command(f"say -1 {message}")

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise RconError("not connected")
        return self._socket

    def _receive(self, sock: socket.socket) -> bytes:
        return sock.recv(4096)


def _command_text(data: bytes) -> str:
    """Decode a command reply, unwrapping BattlEye's multipart header if present.

    A multipart reply starts 0x00 <total> <index>; only the first part is returned, which
    is enough for the commands dzsm issues (`say` replies are empty).
    """
    if len(data) >= 3 and data[0] == 0x00:
        data = data[3:]
    return data.decode("utf-8", errors="replace").strip()
