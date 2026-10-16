"""The persistent RCON session, and the message parsing it feeds.

Driven against a real UDP socket standing in for BattlEye rather than a mocked one. The
whole point of this class is that one thread owns a socket while other threads send
commands through it, and a mock that answers synchronously on the calling thread would
test none of that.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from dzsm.rcon import (
    COMMAND,
    LOGIN,
    LOGIN_OK,
    PREFIX,
    SERVER_MESSAGE,
    RconEvent,
    RconSession,
    build_packet,
    parse_command_reply,
    parse_packet,
    parse_players,
    parse_server_message,
)


class FakeServer:
    """A minimal BattlEye RCON server: accepts a login, answers commands, pushes messages."""

    def __init__(self, *, password: str = "pw") -> None:  # noqa: S107 — a test fixture
        self.password = password
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.25)
        self.port = self.socket.getsockname()[1]

        self.commands: list[str] = []
        self.acked: list[int] = []
        self.replies: dict[str, str] = {}
        self._client: tuple[str, int] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._sequence = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self.socket.close()

    def push(self, text: str) -> None:
        """Send an unsolicited server message, the way BattlEye pushes chat."""
        for _ in range(100):
            if self._client is not None:
                break
            time.sleep(0.02)
        assert self._client is not None, "no client connected to push to"
        sequence = self._sequence & 0xFF
        self._sequence += 1
        payload = bytes([PREFIX, SERVER_MESSAGE, sequence]) + text.encode("utf-8")
        self.socket.sendto(build_packet(payload), self._client)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, address = self.socket.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            self._client = address
            try:
                packet_type, data = parse_packet(raw)
            except Exception:  # noqa: S112 — a fake server ignores junk, like the real one
                continue

            if packet_type == LOGIN:
                ok = data.decode("utf-8", errors="replace") == self.password
                self.socket.sendto(
                    build_packet(bytes([PREFIX, LOGIN, LOGIN_OK if ok else 0x00])), address
                )
            elif packet_type == COMMAND:
                sequence = data[0]
                command = data[1:].decode("utf-8", errors="replace")
                self.commands.append(command)
                self._reply(address, sequence, self.replies.get(command, ""))
            elif packet_type == SERVER_MESSAGE:
                self.acked.append(data[0])

    def _reply(self, address: tuple[str, int], sequence: int, text: str) -> None:
        body = text.encode("utf-8")
        # Split like BattlEye does, so multipart reassembly is exercised for real.
        if len(body) > 64:
            chunks = [body[i : i + 64] for i in range(0, len(body), 64)]
            for index, chunk in enumerate(chunks):
                payload = (
                    bytes([PREFIX, COMMAND, sequence, 0x00, len(chunks), index]) + chunk
                )
                self.socket.sendto(build_packet(payload), address)
            return
        self.socket.sendto(build_packet(bytes([PREFIX, COMMAND, sequence]) + body), address)


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fake = FakeServer()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def session(server: FakeServer) -> Iterator[RconSession]:
    live = RconSession("127.0.0.1", server.port, lambda: "pw", timeout=1.0)
    live.start()
    for _ in range(200):
        if live.status().connected:
            break
        time.sleep(0.02)
    yield live
    live.stop()


def _wait(predicate: object, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.02)
    return False


# --- the connection ----------------------------------------------------------


def test_it_connects_and_logs_in(session: RconSession) -> None:
    assert session.status().connected is True


def test_a_wrong_password_is_reported_not_retried_silently(server: FakeServer) -> None:
    live = RconSession("127.0.0.1", server.port, lambda: "wrong", timeout=0.5)
    live.start()
    try:
        assert _wait(lambda: live.status().last_error is not None)
        assert live.status().connected is False
    finally:
        live.stop()


def test_a_missing_password_is_reported(server: FakeServer) -> None:
    """dzsm generates one into the BattlEye config, so None means that has not happened
    yet — worth saying rather than looping on a connect that cannot work."""
    live = RconSession("127.0.0.1", server.port, lambda: None, timeout=0.5)
    live.start()
    try:
        assert _wait(lambda: live.status().last_error == "no RCON password available")
    finally:
        live.stop()


def test_the_password_is_re_read_on_each_attempt(server: FakeServer) -> None:
    """dzsm can regenerate the BattlEye config; a cached password would never reconnect."""
    passwords = iter(["wrong", "pw", "pw", "pw", "pw", "pw", "pw", "pw"])
    live = RconSession(
        "127.0.0.1", server.port, lambda: next(passwords, "pw"), timeout=0.5
    )
    live.start()
    try:
        assert _wait(lambda: live.status().connected, timeout=6.0)
    finally:
        live.stop()


def test_stopping_is_idempotent(session: RconSession) -> None:
    session.stop()
    session.stop()

    assert session.status().connected is False


# --- commands over the held connection ---------------------------------------


def test_a_command_gets_its_reply(session: FakeServer, server: FakeServer) -> None:
    server.replies["players"] = "Players on server:\n(0 players in total)"

    assert "Players on server" in session.send("players")  # type: ignore[attr-defined]


def test_say_is_sent_as_global_admin_chat(
    session: RconSession, server: FakeServer
) -> None:
    session.say("hello everyone")

    assert _wait(lambda: "say -1 hello everyone" in server.commands)


def test_a_message_to_one_player_uses_their_slot(
    session: RconSession, server: FakeServer
) -> None:
    session.say_to(3, "just you")

    assert _wait(lambda: "say 3 just you" in server.commands)


def test_a_multipart_reply_is_reassembled(
    session: RconSession, server: FakeServer
) -> None:
    """`players` on a populated server does not fit one datagram. Keeping only the first
    part — which this code did until the live list made it matter — truncates the list
    silently somewhere in the middle."""
    long_reply = "\n".join(f"{i}   10.0.0.{i}:2304  10  abc{i}(OK) Player{i}" for i in range(40))
    server.replies["players"] = long_reply

    reply = session.send("players", timeout=5.0)

    assert len(parse_players(reply)) == 40


def test_a_command_with_no_reply_times_out_rather_than_hanging(
    session: RconSession, server: FakeServer
) -> None:
    server.replies.clear()
    # The fake answers every command, so silence is simulated by asking the real thing
    # for something after stopping the server.
    server.stop()

    with pytest.raises(Exception, match="timed out|closed|not running"):
        session.send("players", timeout=0.5)


def test_sending_before_start_is_refused() -> None:
    live = RconSession("127.0.0.1", 1, lambda: "pw")

    with pytest.raises(Exception, match="not running"):
        live.send("players")


# --- pushed messages ---------------------------------------------------------


def test_a_pushed_message_reaches_the_feed(
    session: RconSession, server: FakeServer
) -> None:
    server.push("(Global) Terrylava: hello")

    assert _wait(lambda: session.events_since(0)[1])
    _, events = session.events_since(0)
    assert events[0].kind == "chat"
    assert events[0].text == "hello"


def test_a_pushed_message_is_acknowledged(
    session: RconSession, server: FakeServer
) -> None:
    """BattlEye retransmits anything unacknowledged, which would show the same chat line
    twice in the feed."""
    server.push("(Global) Terrylava: hello")

    assert _wait(lambda: len(server.acked) >= 1)


def test_the_cursor_does_not_repeat_or_skip(
    session: RconSession, server: FakeServer
) -> None:
    server.push("(Global) A: one")
    assert _wait(lambda: session.events_since(0)[1])
    cursor, first = session.events_since(0)

    server.push("(Global) B: two")
    assert _wait(lambda: session.events_since(cursor)[1])
    _, second = session.events_since(cursor)

    assert [e.text for e in first] == ["one"]
    assert [e.text for e in second] == ["two"]


def test_the_event_handler_is_called(server: FakeServer) -> None:
    seen: list[RconEvent] = []
    live = RconSession(
        "127.0.0.1", server.port, lambda: "pw", timeout=1.0, on_event=seen.append
    )
    live.start()
    try:
        assert _wait(lambda: live.status().connected)
        server.push("(Global) A: persist me")
        assert _wait(lambda: seen)
        assert seen[0].text == "persist me"
    finally:
        live.stop()


def test_a_failing_handler_does_not_kill_the_session(server: FakeServer) -> None:
    """Rule 3: a broken persister must not cost the live feed."""

    def explode(event: object) -> None:
        raise RuntimeError("database on fire")

    live = RconSession(
        "127.0.0.1", server.port, lambda: "pw", timeout=1.0, on_event=explode
    )
    live.start()
    try:
        assert _wait(lambda: live.status().connected)
        server.push("(Global) A: one")
        assert _wait(lambda: live.events_since(0)[1])
        server.push("(Global) A: two")
        assert _wait(lambda: len(live.events_since(0)[1]) >= 2)
    finally:
        live.stop()


def test_the_feed_is_a_ring_not_an_archive(server: FakeServer) -> None:
    """It is a live view; anything that must outlive the process is persisted instead."""
    live = RconSession("127.0.0.1", server.port, lambda: "pw", timeout=1.0, buffer=5)
    live.start()
    try:
        assert _wait(lambda: live.status().connected)
        for i in range(12):
            live._record(parse_server_message(f"(Global) A: {i}"))  # noqa: SLF001

        assert len(live.events_since(0)[1]) == 5
    finally:
        live.stop()


# --- keepalive ---------------------------------------------------------------


def test_the_connection_is_kept_alive(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """BattlEye drops a connection idle for 45s, so a held one has to send something."""
    monkeypatch.setattr("dzsm.rcon.KEEPALIVE_SECONDS", 0.1)
    live = RconSession("127.0.0.1", server.port, lambda: "pw", timeout=1.0)
    live.start()
    try:
        assert _wait(lambda: live.status().connected)
        assert _wait(lambda: server.commands.count("") >= 2, timeout=3.0)
    finally:
        live.stop()


# --- message classification --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("(Global) Name: hello", "chat"),
        ("(Side) Name: hello", "chat"),
        ("(Direct) Name: hello", "chat"),
        ("Player #3 Name (1.2.3.4:2304) connected", "connect"),
        ("Player #3 Name disconnected", "disconnect"),
        ("Verified GUID (abc123) of player #3 Name", "guid"),
        ("Player #3 Name has been kicked by BattlEye: Admin Kick", "kick"),
        ("RCon admin #1 (1.2.3.4:5678) logged in", "admin"),
        ("something nobody has seen before", "other"),
    ],
)
def test_messages_are_classified(raw: str, kind: str) -> None:
    assert parse_server_message(raw).kind == kind


def test_chat_is_split_into_name_and_text() -> None:
    event = parse_server_message("(Global) Terrylava: watch out at the coast")

    assert event.channel == "Global"
    assert event.name == "Terrylava"
    assert event.text == "watch out at the coast"


def test_a_colon_in_the_message_survives() -> None:
    event = parse_server_message("(Global) Terrylava: meet at 10:30 by the church")

    assert event.text == "meet at 10:30 by the church"


def test_a_connect_from_a_player_named_disconnected_is_not_a_departure() -> None:
    """Names are attacker-controlled. Checking connect first is what stops someone
    called "x disconnected" showing up as leaving every time they arrive."""
    event = parse_server_message("Player #3 x disconnected (1.2.3.4:2304) connected")

    assert event.kind == "connect"


def test_an_unrecognised_line_keeps_its_text() -> None:
    """Dropping it would lose a chat line to a format change, which is worse than showing
    it unparsed."""
    event = parse_server_message("Some new BattlEye notice")

    assert event.kind == "other"
    assert event.raw == "Some new BattlEye notice"


# --- the wire format ---------------------------------------------------------


def test_a_single_part_reply_is_recognised() -> None:
    assert parse_command_reply(bytes([7]) + b"hello") == (7, 1, 0, b"hello")


def test_a_multipart_header_is_recognised() -> None:
    assert parse_command_reply(bytes([7, 0x00, 3, 1]) + b"part") == (7, 3, 1, b"part")
