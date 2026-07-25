"""BattlEye RCON.

The framing is tested against literal bytes because it fails silently: a wrong CRC gets
no reply at all rather than an error, so an encoding bug would look exactly like an
unreachable server. The client is then exercised against a real UDP socket standing in
for BattlEye.
"""

from __future__ import annotations

import socket
import threading
import zlib

import pytest

from dzsm.rcon import (
    COMMAND,
    LOGIN,
    SERVER_MESSAGE,
    RconClient,
    RconError,
    build_packet,
    parse_packet,
)

PASSWORD = "letmein"  # noqa: S105 — a test fixture


# --- framing -----------------------------------------------------------------


def test_packet_starts_with_be_and_a_little_endian_crc() -> None:
    payload = b"\xff\x00secret"
    packet = build_packet(payload)

    assert packet[:2] == b"BE"
    assert int.from_bytes(packet[2:6], "little") == zlib.crc32(payload)
    assert packet[6:] == payload


def test_roundtrip() -> None:
    packet = build_packet(bytes([0xFF, COMMAND, 7]) + b"players")
    packet_type, data = parse_packet(packet)

    assert packet_type == COMMAND
    assert data[0] == 7
    assert data[1:] == b"players"


def test_a_corrupted_payload_is_rejected() -> None:
    """A silent accept here would report a mangled reply as a successful command."""
    packet = bytearray(build_packet(b"\xff\x01\x00ok"))
    packet[-1] ^= 0xFF

    with pytest.raises(RconError, match="CRC mismatch"):
        parse_packet(bytes(packet))


def test_a_foreign_packet_is_rejected() -> None:
    with pytest.raises(RconError, match="not a BattlEye packet"):
        parse_packet(b"HTTP/1.1 200 OK")


def test_a_truncated_packet_is_rejected() -> None:
    with pytest.raises(RconError, match="not a BattlEye packet"):
        parse_packet(b"BE\x00\x00")


def test_a_payload_without_the_ff_prefix_is_rejected() -> None:
    with pytest.raises(RconError, match="0xFF"):
        parse_packet(build_packet(b"\x00\x01nope"))


# --- a stand-in BattlEye server ----------------------------------------------


class FakeBattlEye:
    """Speaks just enough of the protocol to exercise the client."""

    def __init__(self, *, password: str = PASSWORD, interleave_message: bool = False) -> None:
        self.password = password
        self.interleave_message = interleave_message
        self.commands: list[str] = []
        self.acknowledged: list[int] = []
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.settimeout(5.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def port(self) -> int:
        return int(self._socket.getsockname()[1])

    def __enter__(self) -> FakeBattlEye:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                packet, address = self._socket.recvfrom(4096)
            except OSError:
                return
            try:
                packet_type, data = parse_packet(packet)
            except RconError:
                continue

            if packet_type == LOGIN:
                ok = data.decode() == self.password
                self._socket.sendto(
                    build_packet(bytes([0xFF, LOGIN, 0x01 if ok else 0x00])), address
                )
            elif packet_type == COMMAND:
                sequence, command = data[0], data[1:].decode()
                self.commands.append(command)
                if self.interleave_message:
                    # BattlEye interleaves these with replies; the client must ack and
                    # keep waiting rather than treating it as the answer.
                    self._socket.sendto(
                        build_packet(bytes([0xFF, SERVER_MESSAGE, 42]) + b"player joined"),
                        address,
                    )
                self._socket.sendto(
                    build_packet(bytes([0xFF, COMMAND, sequence]) + b"done"), address
                )
            elif packet_type == SERVER_MESSAGE:
                self.acknowledged.append(data[0])


def test_login_and_command() -> None:
    with FakeBattlEye() as server, RconClient("127.0.0.1", server.port, PASSWORD) as client:
        assert client.command("players") == "done"
        assert server.commands == ["players"]


def test_say_uses_global_admin_chat() -> None:
    """`say -1` is the broadcast channel; anything else goes to one player."""
    with FakeBattlEye() as server, RconClient("127.0.0.1", server.port, PASSWORD) as client:
        client.say("Server restart in 2 minutes")

    assert server.commands == ["say -1 Server restart in 2 minutes"]


def test_a_wrong_password_is_refused_clearly() -> None:
    with FakeBattlEye() as server:
        with pytest.raises(RconError, match="login refused"):
            RconClient("127.0.0.1", server.port, "wrong").connect()


def test_server_messages_are_acknowledged_not_mistaken_for_replies() -> None:
    """Without the ack BattlEye retransmits, and without the skip the client would
    return 'player joined' as the command's answer."""
    with FakeBattlEye(interleave_message=True) as server:
        with RconClient("127.0.0.1", server.port, PASSWORD) as client:
            assert client.command("players") == "done"
        assert server.acknowledged == [42]


def test_sequence_numbers_advance() -> None:
    with FakeBattlEye() as server, RconClient("127.0.0.1", server.port, PASSWORD) as client:
        client.command("one")
        client.command("two")

    assert server.commands == ["one", "two"]


def test_command_before_connect_fails() -> None:
    with pytest.raises(RconError, match="not connected"):
        RconClient("127.0.0.1", 1, PASSWORD).command("players")


def test_an_unreachable_port_raises_rather_than_hanging() -> None:
    client = RconClient("127.0.0.1", 1, PASSWORD, timeout=0.3)
    with pytest.raises(RconError, match="connect"):
        client.connect()
