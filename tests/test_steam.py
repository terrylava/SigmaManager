"""Steam Web API parsing and download verification.

No network: the API is exercised through httpx.MockTransport, and verification through
real temp directories. The point of these tests is the thing that killed om.py —
steamcmd exiting 0 on a failed download.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from dzsm.config import Config
from dzsm.steam import (
    DownloadResult,
    SteamError,
    SteamGuardRequired,
    check_updates,
    verify_download,
)


def _client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _details_response(entries: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(200, json={"response": {"publishedfiledetails": entries}})


def _ok(workshop_id: int, time_updated: int) -> dict[str, object]:
    return {"publishedfileid": str(workshop_id), "result": 1, "time_updated": time_updated}


# --- check_updates -----------------------------------------------------------


def test_check_updates_with_no_ids_returns_empty_dict() -> None:
    """Rule 1: an empty request is an empty collection, not None or False."""
    assert check_updates([]) == {}


def test_check_updates_parses_time_updated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _details_response([_ok(1559212036, 1721005200), _ok(2116157322, 1719878400)])

    with _client(handler) as client:
        result = check_updates([1559212036, 2116157322], client=client)
    assert result == {1559212036: 1721005200, 2116157322: 1719878400}


def test_check_updates_sends_the_documented_form_encoding() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for pair in request.content.decode().split("&"):
            key, _, value = pair.partition("=")
            seen[httpx.QueryParams(f"k={key}")["k"]] = value
        return _details_response([_ok(42, 1)])

    with _client(handler) as client:
        check_updates([42], client=client)
    assert seen["itemcount"] == "1"
    assert seen["publishedfileids[0]"] == "42"


def test_check_updates_omits_failed_ids_rather_than_aborting() -> None:
    """A dead workshop id must not take the whole batch down with it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _details_response(
            [_ok(1, 100), {"publishedfileid": "2", "result": 9}, _ok(3, 300)]
        )

    with _client(handler) as client:
        result = check_updates([1, 2, 3], client=client)
    assert result == {1: 100, 3: 300}


def test_check_updates_omits_entries_with_no_time_updated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _details_response([{"publishedfileid": "7", "result": 1}])

    with _client(handler) as client:
        assert check_updates([7], client=client) == {}


def test_check_updates_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _client(handler) as client, pytest.raises(SteamError, match="request failed"):
        check_updates([1], client=client)


def test_check_updates_raises_on_malformed_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": {}})

    with _client(handler) as client, pytest.raises(SteamError, match="publishedfiledetails"):
        check_updates([1], client=client)


def test_check_updates_raises_on_non_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    with _client(handler) as client, pytest.raises(SteamError, match="non-JSON"):
        check_updates([1], client=client)


# --- verify_download ---------------------------------------------------------


def _result(
    config: Config,
    *,
    workshop_id: int = 1559212036,
    returncode: int = 0,
    output: str = "Success. Downloaded item.",
    before: float | None = 1000.0,
    after: float | None = 2000.0,
) -> DownloadResult:
    return DownloadResult(
        workshop_id=workshop_id,
        path=config.workshop_download_dir / str(workshop_id),
        returncode=returncode,
        output=output,
        acf_mtime_before=before,
        acf_mtime_after=after,
    )


def test_verify_passes_on_a_good_download(config: Config, downloaded_mod: Path) -> None:
    verify_download(config, _result(config))


def test_verify_rejects_missing_directory_despite_exit_zero(config: Config) -> None:
    """The om.py failure mode: steamcmd says success, nothing is on disk."""
    with pytest.raises(SteamError, match="does not exist"):
        verify_download(config, _result(config))


def test_verify_rejects_empty_directory_despite_exit_zero(config: Config) -> None:
    (config.workshop_download_dir / "1559212036").mkdir(parents=True)
    config.workshop_acf.write_text("", encoding="utf-8")
    with pytest.raises(SteamError, match="contains no files"):
        verify_download(config, _result(config))


def test_verify_rejects_a_stale_manifest(config: Config, downloaded_mod: Path) -> None:
    """Exit 0, files present, but steamcmd did no work — the manifest never moved."""
    with pytest.raises(SteamError, match="did not advance"):
        verify_download(config, _result(config, before=1000.0, after=1000.0))


def test_verify_rejects_a_missing_manifest(config: Config, downloaded_mod: Path) -> None:
    with pytest.raises(SteamError, match="manifest"):
        verify_download(config, _result(config, after=None))


def test_verify_accepts_a_first_ever_download(config: Config, downloaded_mod: Path) -> None:
    """No manifest before the run is normal on a fresh install, not a failure."""
    verify_download(config, _result(config, before=None, after=2000.0))


def test_verify_rejects_nonzero_exit(config: Config, downloaded_mod: Path) -> None:
    with pytest.raises(SteamError, match="exited 3"):
        verify_download(config, _result(config, returncode=3))


@pytest.mark.parametrize(
    "marker",
    ["Timeout downloading item", "Rate Limit Exceeded", "ERROR! Download item", "No subscription"],
)
def test_verify_rejects_known_error_markers(
    config: Config, downloaded_mod: Path, marker: str
) -> None:
    """These appear in output while steamcmd still exits 0."""
    with pytest.raises(SteamError, match="steamcmd reported"):
        verify_download(config, _result(config, output=f"...\n{marker} 1559212036\n..."))


def test_verify_raises_steam_guard_with_actionable_advice(
    config: Config, downloaded_mod: Path
) -> None:
    with pytest.raises(SteamGuardRequired, match="steamcmd \\+login"):
        verify_download(config, _result(config, output="Steam Guard code required"))


def test_steam_guard_is_a_steam_error(config: Config, downloaded_mod: Path) -> None:
    """Callers that catch SteamError must not miss the Guard case."""
    with pytest.raises(SteamError):
        verify_download(config, _result(config, output="Two-factor code needed"))


def test_error_message_includes_output_tail_for_diagnosis(
    config: Config, downloaded_mod: Path
) -> None:
    """Rule 3: a failure must carry enough context to diagnose without a rerun."""
    output = "\n".join(f"line {i}" for i in range(50)) + "\nRate Limit Exceeded"
    with pytest.raises(SteamError) as excinfo:
        verify_download(config, _result(config, output=output))
    assert "line 49" in str(excinfo.value)
    assert "line 10" not in str(excinfo.value)
