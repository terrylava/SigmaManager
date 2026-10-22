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
import queue
import re
import socket
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class PlayerSlot:
    """One connected player, as BattlEye's `players` command reports them.

    `guid` is BattlEye's own identifier. It is **not** the id the DayZ admin log records,
    which is a different hash of a different thing — the two cannot be joined, which is
    why the live view and the session ledger are kept as separate things rather than
    pretending to be one dataset keyed by player.

    `ip` is carried so the live view can show it and deliberately never persisted: it is
    the only personal data in the set and nothing here needs a durable record of it.
    """

    number: int
    ip: str
    port: int
    ping: int
    guid: str
    name: str
    # BattlEye appends "(OK)" once it has verified the GUID, "(?)" while it has not.
    verified: bool
    # Connected but still loading, which BattlEye marks with a "(Lobby)" suffix.
    lobby: bool


# 0   192.168.1.5:2304      31   d41d8cd9...f8427e(OK) SomeName
_PLAYER_ROW = re.compile(
    r"^(?P<number>\d+)\s+"
    r"(?P<ip>[0-9a-fA-F.:]+):(?P<port>\d+)\s+"
    r"(?P<ping>-?\d+)\s+"
    r"(?P<guid>[0-9a-f]+)\((?P<status>[^)]*)\)\s+"
    r"(?P<name>.*)$"
)


@dataclass(frozen=True)
class SessionStatus:
    """What a persistent session is currently doing, for display."""

    connected: bool
    since: float | None
    last_error: str | None
    events_seen: int
    attempts: int


class RconSession:
    """A persistent RCON connection, so BattlEye's pushed messages arrive live.

    The one-shot RconClient above cannot see them: BattlEye pushes connects, disconnects
    and **chat** to whoever is connected, and there is no command that returns any of it
    after the fact. Polling can count players; only a held connection can hear them talk.

    One thread owns the socket and does all sending and receiving. Commands from other
    threads go through a queue and wait for their reply, rather than writing to the socket
    directly — a second writer racing the receive loop over a reconnect is the kind of bug
    that shows up once a week and never in a test.

    Reconnects on its own with backoff, re-reading the password each time through
    `password_provider`, because dzsm can regenerate the BattlEye config and a cached
    password would then be wrong forever.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password_provider: Callable[[], str | None],
        *,
        timeout: float = 5.0,
        buffer: int = 1000,
        on_event: Callable[[RconEvent], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._password_provider = password_provider
        self._timeout = timeout
        self._on_event = on_event

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Events are kept in a ring: this is a live feed, not an archive. Anything that
        # must outlive the process is persisted by on_event.
        self._events: deque[tuple[int, RconEvent]] = deque(maxlen=buffer)
        self._cursor = 0

        self._outbox: queue.Queue[_Pending] = queue.Queue()
        self._pending: dict[int, _Pending] = {}
        self._sequence = 0

        self._connected = False
        self._since: float | None = None
        self._last_error: str | None = None
        self._attempts = 0

    # --- lifecycle ---

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"rcon-{self.port}", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)

    def status(self) -> SessionStatus:
        with self._lock:
            return SessionStatus(
                connected=self._connected,
                since=self._since,
                last_error=self._last_error,
                events_seen=self._cursor,
                attempts=self._attempts,
            )

    # --- public API ---

    def events_since(self, cursor: int) -> tuple[int, list[RconEvent]]:
        """Events newer than `cursor`, plus the new cursor.

        A cursor rather than a timestamp, so a client polling fast never sees the same
        line twice and never misses one that arrived within the same second.
        """
        with self._lock:
            fresh = [event for index, event in self._events if index > cursor]
            return self._cursor, fresh

    def send(self, command: str, *, timeout: float = 5.0) -> str:
        """Run a command on the held connection and return its reply."""
        if self._thread is None:
            raise RconError("the live RCON session is not running")
        pending = _Pending(command=command)
        self._outbox.put(pending)
        if not pending.done.wait(timeout):
            with self._lock:
                self._pending.pop(pending.sequence, None)
            raise RconError(f"RCON command {command!r} timed out on the live session")
        if pending.error is not None:
            raise RconError(pending.error)
        return pending.reply

    def say(self, message: str) -> None:
        self.send(f"say -1 {message}")

    def say_to(self, player_number: int, message: str) -> None:
        """Message one player. BattlEye addresses them by their current slot number."""
        self.send(f"say {player_number} {message}")

    # --- the loop ---

    def _loop(self) -> None:
        backoff = 1.0
        sock: socket.socket | None = None
        last_sent = 0.0

        while not self._stop.is_set():
            if sock is None:
                sock = self._try_connect()
                if sock is None:
                    # Capped: a server that is simply down should cost one attempt every
                    # half minute, not a tight loop against a closed port.
                    if self._stop.wait(backoff):
                        break
                    backoff = min(backoff * 2, 30.0)
                    continue
                backoff = 1.0
                last_sent = time.monotonic()

            try:
                last_sent = self._pump_outbox(sock, last_sent)
                if time.monotonic() - last_sent > KEEPALIVE_SECONDS:
                    self._send_command(sock, _Pending(command=""), keepalive=True)
                    last_sent = time.monotonic()
                self._pump_socket(sock)
            except (OSError, RconError) as exc:
                self._mark_disconnected(f"{exc}")
                sock.close()
                sock = None

        if sock is not None:
            sock.close()
        self._mark_disconnected(None)

    def _try_connect(self) -> socket.socket | None:
        password = self._password_provider()
        with self._lock:
            self._attempts += 1
            attempt = self._attempts
        if not password:
            self._mark_disconnected("no RCON password available")
            return None

        try:
            client = RconClient(self.host, self.port, password, timeout=self._timeout)
            client.connect()
        except RconError as exc:
            # Only the first failure is worth a warning: a server that is down will
            # produce one of these every backoff interval forever.
            if attempt <= 1:
                _log.warning("rcon: live session to %s:%s failed: %s", self.host, self.port, exc)
            else:
                _log.debug("rcon: live session to %s:%s failed: %s", self.host, self.port, exc)
            self._mark_disconnected(str(exc))
            return None

        sock = client._socket  # noqa: SLF001 — the session takes ownership of the socket
        if sock is None:
            self._mark_disconnected("connected but no socket")
            return None
        # Short, so the loop can service the outbox and the keepalive promptly rather
        # than blocking for the full command timeout on a quiet server.
        sock.settimeout(0.25)
        with self._lock:
            self._connected = True
            self._since = time.time()
            self._last_error = None
        _log.info("rcon: live session connected to %s:%s", self.host, self.port)
        return sock

    def _pump_outbox(self, sock: socket.socket, last_sent: float) -> float:
        while True:
            try:
                pending = self._outbox.get_nowait()
            except queue.Empty:
                return last_sent
            self._send_command(sock, pending)
            last_sent = time.monotonic()

    def _send_command(
        self, sock: socket.socket, pending: _Pending, *, keepalive: bool = False
    ) -> None:
        with self._lock:
            sequence = self._sequence & 0xFF
            self._sequence += 1
            pending.sequence = sequence
            if not keepalive:
                self._pending[sequence] = pending
        payload = bytes([PREFIX, COMMAND, sequence]) + pending.command.encode("utf-8")
        sock.send(build_packet(payload))

    def _pump_socket(self, sock: socket.socket) -> None:
        try:
            raw = sock.recv(4096)
        except TimeoutError:
            return
        packet_type, data = parse_packet(raw)

        if packet_type == SERVER_MESSAGE and data:
            # Ack first: an unacknowledged message is retransmitted, and the duplicate
            # would show up in the feed as a repeated chat line.
            sock.send(build_packet(bytes([PREFIX, SERVER_MESSAGE, data[0]])))
            self._record(parse_server_message(data[1:].decode("utf-8", errors="replace")))
            return

        if packet_type == COMMAND and data:
            sequence, total, index, part = parse_command_reply(data)
            with self._lock:
                pending = self._pending.get(sequence)
            if pending is None:
                return  # a keepalive's reply, or one that already timed out
            if total <= 1:
                pending.finish(_command_text(part))
            else:
                if pending.multipart is None:
                    pending.multipart = _Multipart(total)
                pending.multipart.add(index, part)
                if pending.multipart.complete:
                    pending.finish(pending.multipart.text())
            if pending.done.is_set():
                with self._lock:
                    self._pending.pop(sequence, None)

    def _record(self, event: RconEvent) -> None:
        with self._lock:
            self._cursor += 1
            self._events.append((self._cursor, event))
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                # A failing persister must not kill the session or lose the live feed
                # (Rule 3). Logged with the event so it is not silently gone.
                _log.exception("rcon: event handler failed for %r", event.raw)

    def _mark_disconnected(self, error: str | None) -> None:
        with self._lock:
            self._connected = False
            self._since = None
            if error is not None:
                self._last_error = error
            # Anything waiting will never get its reply on this socket.
            for pending in self._pending.values():
                pending.fail(error or "RCON session closed")
            self._pending.clear()


class _Pending:
    """A command waiting for its reply, handed between the caller and the session loop."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.sequence = -1
        self.done = threading.Event()
        self.reply = ""
        self.error: str | None = None
        self.multipart: _Multipart | None = None

    def finish(self, reply: str) -> None:
        self.reply = reply
        self.done.set()

    def fail(self, error: str) -> None:
        self.error = error
        self.done.set()


BANS_COMMAND = "bans"


def kick_command(player_number: int, reason: str = "") -> str:
    """Disconnect one player. They can come straight back.

    Addressed by the slot number BattlEye is using *now*, and it reuses those as people
    leave — so a number read from a stale list can reach the wrong person. Callers
    should be acting on a list they have just fetched.
    """
    return f"kick {player_number} {reason}".rstrip()


def ban_command(player_number: int, minutes: int = 0, reason: str = "") -> str:
    """Ban a connected player by slot. 0 minutes is permanent, BattlEye's convention.

    The ban is recorded against their GUID, not their IP, so this does not turn the live
    view's addresses into anything durable — the rule that IPs are shown and never
    persisted still holds.
    """
    return f"ban {player_number} {minutes} {reason}".rstrip()


def unban_command(ban_id: int) -> str:
    """Remove one ban by the number `bans` gives it.

    Those numbers are positions in the list and shift when anything is removed, so the
    caller should be acting on a listing it has just read.
    """
    return f"removeBan {ban_id}"


def parse_players(text: str) -> list[PlayerSlot]:
    """Parse the `players` command's table.

    Deliberately line-by-line and tolerant: it skips the header, the dashes and the
    trailing count rather than depending on their exact wording, because the only thing
    worth relying on here is the shape of a row. A format change should cost a player
    row, not raise.
    """
    players: list[PlayerSlot] = []
    for line in text.splitlines():
        match = _PLAYER_ROW.match(line.strip())
        if match is None:
            continue
        name = match["name"].strip()
        lobby = name.endswith("(Lobby)")
        if lobby:
            name = name[: -len("(Lobby)")].strip()
        players.append(
            PlayerSlot(
                number=int(match["number"]),
                ip=match["ip"],
                port=int(match["port"]),
                ping=int(match["ping"]),
                guid=match["guid"],
                name=name,
                verified=match["status"].upper() == "OK",
                lobby=lobby,
            )
        )
    return players


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
        assembled: _Multipart | None = None
        try:
            sock.send(build_packet(payload))
            # Generous, because a multipart reply is several packets and each may be
            # preceded by server messages that have to be acked and skipped.
            for _ in range(64):
                packet_type, data = parse_packet(self._receive(sock))
                if packet_type == SERVER_MESSAGE and data:
                    sock.send(build_packet(bytes([PREFIX, SERVER_MESSAGE, data[0]])))
                    continue
                if packet_type == COMMAND and data and data[0] == sequence:
                    _, total, index, part = parse_command_reply(data)
                    if total <= 1:
                        return _command_text(part)
                    if assembled is None:
                        assembled = _Multipart(total)
                    assembled.add(index, part)
                    if assembled.complete:
                        return assembled.text()
        except (OSError, RconError) as exc:
            raise RconError(f"RCON command {command!r} failed: {exc}") from exc

        raise RconError(f"RCON command {command!r} got no matching reply")

    def keepalive(self) -> None:
        """Empty command, purely to stop BattlEye dropping an idle connection."""
        self.command("")

    def say(self, message: str) -> None:
        """Broadcast to every player. `say -1` is BattlEye's global admin chat."""
        self.command(f"say -1 {message}")

    def say_to(self, player_number: int, message: str) -> None:
        """Message one player. BattlEye addresses them by their current slot number."""
        self.command(f"say {player_number} {message}")

    def players(self) -> list[PlayerSlot]:
        """Who is connected right now.

        The only live view that works regardless of `-adminlog`, and the only source of
        ping. Returns a list — empty for an empty server, never None (Rule 1).
        """
        return parse_players(self.command("players"))

    def kick(self, player_number: int, reason: str = "") -> str:
        """Disconnect one player. They can come straight back."""
        return self.command(kick_command(player_number, reason))

    def ban(self, player_number: int, minutes: int = 0, reason: str = "") -> str:
        """Ban a connected player by slot, and disconnect them."""
        return self.command(ban_command(player_number, minutes, reason))

    def bans(self) -> str:
        """BattlEye's ban list, as it prints it."""
        return self.command(BANS_COMMAND)

    def unban(self, ban_id: int) -> str:
        """Remove one ban by the number `bans` gives it."""
        return self.command(unban_command(ban_id))

    def shutdown(self) -> None:
        """Ask the server to stop.

        Lets DayZ close down on its own terms and flush persistence, rather than being
        signalled from outside. The server usually dies before replying, so a missing
        reply is not treated as failure by callers.
        """
        self.command("#shutdown")

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise RconError("not connected")
        return self._socket

    def _receive(self, sock: socket.socket) -> bytes:
        return sock.recv(4096)


@dataclass(frozen=True)
class RconEvent:
    """One message BattlEye pushed to us.

    `raw` is always the server's text verbatim. `kind` and the extracted fields are a
    best-effort classification on top of it — the formats are BattlEye's and undocumented,
    so an unrecognised line becomes kind "other" with its text intact rather than being
    dropped. Losing a chat line to a format change would be worse than showing it unparsed.
    """

    kind: str  # chat | connect | disconnect | guid | kick | admin | other
    raw: str
    name: str | None = None
    # For chat: Global, Side, Vehicle, Direct, Group.
    channel: str | None = None
    text: str | None = None


# (Global) SomeName: hello there
_CHAT = re.compile(r"^\((?P<channel>Global|Side|Vehicle|Direct|Group|Command)\)\s+(?P<rest>.*)$")
# Player #3 SomeName (1.2.3.4:2304) connected
_CONNECT = re.compile(r"^Player #(?P<number>\d+) (?P<name>.*) \((?P<ip>[^)]*)\) connected")
# Player #3 SomeName disconnected
_DISCONNECT = re.compile(r"^Player #(?P<number>\d+) (?P<name>.*) disconnected")
# Verified GUID (abc...) of player #3 SomeName
_GUID = re.compile(r"^Verified GUID \((?P<guid>[^)]*)\) of player #(?P<number>\d+) (?P<name>.*)")
_KICK = re.compile(r"^Player #(?P<number>\d+) (?P<name>.*?) (?:has been kicked|was kicked)")


def parse_server_message(raw: str) -> RconEvent:
    """Classify a pushed server message.

    Chat is the one that matters most: there is no command that returns chat history, so a
    line missed here is gone for good.
    """
    text = raw.strip()

    chat = _CHAT.match(text)
    if chat is not None:
        rest = chat["rest"]
        name, separator, body = rest.partition(":")
        # Without a colon it is not a player line (BattlEye uses this shape for some of
        # its own notices), so the whole thing is kept as the message.
        return RconEvent(
            kind="chat",
            raw=text,
            channel=chat["channel"],
            name=name.strip() if separator else None,
            text=body.strip() if separator else rest.strip(),
        )

    for pattern, kind in ((_GUID, "guid"), (_KICK, "kick"), (_CONNECT, "connect")):
        match = pattern.match(text)
        if match is not None:
            return RconEvent(kind=kind, raw=text, name=match["name"].strip())

    # Checked after connect, because a player can name themselves "x disconnected": the
    # connect line for them would otherwise match the disconnect pattern first and be
    # reported as a player leaving. Names are attacker-controlled throughout here.
    disconnect = _DISCONNECT.match(text)
    if disconnect is not None:
        return RconEvent(kind="disconnect", raw=text, name=disconnect["name"].strip())

    if text.startswith("RCon admin"):
        return RconEvent(kind="admin", raw=text)

    return RconEvent(kind="other", raw=text)


def parse_command_reply(data: bytes) -> tuple[int, int, int, bytes]:
    """Split a command reply into (sequence, total parts, this index, payload).

    BattlEye splits a reply longer than one datagram, marking the parts with
    `0x00 <total> <index>` after the sequence byte. A single-part reply has no marker.

    The ambiguity is inherent to the protocol: a one-part payload beginning with a NUL
    byte would be misread as a marker. Command replies are text, so that does not arise.
    """
    sequence = data[0]
    rest = data[1:]
    if len(rest) >= 3 and rest[0] == 0x00:
        return sequence, rest[1], rest[2], rest[3:]
    return sequence, 1, 0, rest


class _Multipart:
    """Reassembles a split command reply.

    Needed because `players` on a populated server does not fit in one datagram. Keeping
    only the first part — which is what this code did until the live player list made it
    matter — silently truncates the list somewhere in the middle.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.parts: dict[int, bytes] = {}

    def add(self, index: int, payload: bytes) -> None:
        self.parts[index] = payload

    @property
    def complete(self) -> bool:
        return len(self.parts) >= self.total

    def text(self) -> str:
        joined = b"".join(self.parts[index] for index in sorted(self.parts))
        return joined.decode("utf-8", errors="replace").strip()


def _command_text(data: bytes) -> str:
    """Decode a single-part command reply's payload."""
    return data.decode("utf-8", errors="replace").strip()
