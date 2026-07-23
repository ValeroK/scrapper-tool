"""Unit tests for the obscura batch fast path (D4).

The subprocess is always faked — spawning a real browser binary in a unit test
would be slow and would only pass on machines that happen to have obscura
installed. What's actually worth testing here is the parsing and the failure
handling, because the CLI is a young external binary and the tempting
implementation (assume one output shape, treat non-zero exit as fatal) throws away
good pages.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from scrapper_tool.crawl.batch import (
    batch_fetch,
    obscura_available,
    parse_batch_output,
)
from scrapper_tool.errors import ConfigurationError

_URLS = ["https://a.test/1", "https://a.test/2"]


class _FakeProcess:
    """Stand-in for asyncio's subprocess handle."""

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        *,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


def _fake_exec(
    monkeypatch: pytest.MonkeyPatch, process: _FakeProcess, *, on_path: bool = True
) -> list[list[str]]:
    """Replace subprocess spawning. Returns the captured argv list."""
    calls: list[list[str]] = []

    async def create(*args: str, **_kwargs: Any) -> _FakeProcess:
        calls.append(list(args))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        "scrapper_tool.crawl.batch.shutil.which",
        lambda name: "/usr/bin/obscura" if on_path else None,
    )
    return calls


# --- parsing ----------------------------------------------------------------


def test_parses_a_json_array() -> None:
    raw = json.dumps(
        [
            {"url": "https://a.test/1", "html": "<p>one</p>", "status": 200},
            {"url": "https://a.test/2", "html": "<p>two</p>", "status": 200},
        ]
    )
    result = parse_batch_output(raw, _URLS)
    assert result.ok_count == 2
    assert [p.url for p in result.pages] == _URLS
    assert result.missing_urls == ()


def test_parses_json_lines() -> None:
    """One record per line is an equally plausible output shape."""
    raw = "\n".join(json.dumps({"url": url, "html": "<p>x</p>", "status": 200}) for url in _URLS)
    result = parse_batch_output(raw, _URLS)
    assert result.ok_count == 2


def test_parses_a_single_object() -> None:
    raw = json.dumps({"url": "https://a.test/1", "html": "<p>one</p>", "status": 200})
    result = parse_batch_output(raw, ["https://a.test/1"])
    assert result.ok_count == 1


def test_tolerates_alternative_field_names() -> None:
    """The CLI's field names aren't a contract we control."""
    raw = json.dumps(
        [{"requestedUrl": "https://a.test/1", "content": "<p>x</p>", "statusCode": 200}]
    )
    result = parse_batch_output(raw, ["https://a.test/1"])
    assert result.pages[0].url == "https://a.test/1"
    assert result.pages[0].html == "<p>x</p>"
    assert result.pages[0].status == 200


def test_partial_garbage_keeps_the_good_records() -> None:
    """A strict parse would discard the whole batch over one bad line."""
    raw = (
        json.dumps({"url": "https://a.test/1", "html": "<p>x</p>"})
        + "\nnot json at all\n"
        + json.dumps({"url": "https://a.test/2", "html": "<p>y</p>"})
    )
    result = parse_batch_output(raw, _URLS)
    assert result.ok_count == 2
    assert result.unparsed_records == 1


def test_a_record_without_a_url_is_counted_not_kept() -> None:
    raw = json.dumps([{"html": "<p>orphan</p>"}])
    result = parse_batch_output(raw, _URLS)
    assert result.pages == []
    assert result.unparsed_records == 1


def test_urls_the_cli_never_returned_are_reported() -> None:
    """Silence about a missing URL reads as success. It isn't."""
    raw = json.dumps([{"url": "https://a.test/1", "html": "<p>x</p>"}])
    result = parse_batch_output(raw, _URLS)
    assert result.missing_urls == ("https://a.test/2",)


def test_per_url_errors_are_surfaced() -> None:
    raw = json.dumps([{"url": "https://a.test/1", "error": "navigation timeout"}])
    result = parse_batch_output(raw, ["https://a.test/1"])
    assert result.pages[0].ok is False
    assert result.pages[0].error == "navigation timeout"


def test_empty_output() -> None:
    result = parse_batch_output("", _URLS)
    assert result.pages == []
    assert result.missing_urls == tuple(_URLS)


def test_non_numeric_status_does_not_raise() -> None:
    raw = json.dumps([{"url": "https://a.test/1", "html": "<p>x</p>", "status": "OK"}])
    result = parse_batch_output(raw, ["https://a.test/1"])
    assert result.pages[0].status == 0
    assert result.pages[0].ok is True, "an unreadable status must not invalidate the HTML"


# --- invocation -------------------------------------------------------------


@pytest.mark.asyncio
async def test_builds_the_expected_command(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps([{"url": u, "html": "<p>x</p>", "status": 200} for u in _URLS])
    calls = _fake_exec(monkeypatch, _FakeProcess(stdout=payload.encode()))

    result = await batch_fetch(_URLS, concurrency=5, proxy="http://p:8080")

    assert result.ok_count == 2
    argv = calls[0]
    assert argv[:2] == ["obscura", "scrape"]
    assert argv[2:4] == _URLS
    assert "--concurrency" in argv
    assert argv[argv.index("--concurrency") + 1] == "5"
    assert "--stealth" in argv
    assert argv[argv.index("--proxy") + 1] == "http://p:8080"
    assert "--format" in argv


@pytest.mark.asyncio
async def test_stealth_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_exec(monkeypatch, _FakeProcess(stdout=b"[]"))
    await batch_fetch(_URLS, stealth=False)
    assert "--stealth" not in calls[0]


@pytest.mark.asyncio
async def test_missing_cli_raises_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Choosing this path and not having the binary is a misconfiguration.

    Silently falling back would hide that the fast path never ran.
    """
    _fake_exec(monkeypatch, _FakeProcess(), on_path=False)
    with pytest.raises(ConfigurationError, match="obscura"):
        await batch_fetch(_URLS)


@pytest.mark.asyncio
async def test_empty_url_list_does_not_spawn_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_exec(monkeypatch, _FakeProcess())
    result = await batch_fetch([])
    assert result.requested == 0
    assert calls == []


@pytest.mark.asyncio
async def test_nonzero_exit_still_returns_parsed_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """One 404 in a batch of 50 must not discard the other 49."""
    payload = json.dumps([{"url": "https://a.test/1", "html": "<p>x</p>", "status": 200}])
    _fake_exec(
        monkeypatch,
        _FakeProcess(stdout=payload.encode(), stderr=b"1 url failed", returncode=1),
    )
    result = await batch_fetch(_URLS)
    assert result.ok_count == 1
    assert result.missing_urls == ("https://a.test/2",)


@pytest.mark.asyncio
async def test_timeout_kills_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """An orphaned browser per timed-out batch leaks until handles run out."""
    process = _FakeProcess(hang=True)
    _fake_exec(monkeypatch, process)

    result = await batch_fetch(_URLS, timeout_s=0.01)

    assert process.killed is True
    assert process.waited is True
    assert result.pages == []
    assert result.missing_urls == tuple(_URLS)


def test_obscura_available_reflects_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scrapper_tool.crawl.batch.shutil.which", lambda name: None)
    assert obscura_available() is False
    monkeypatch.setattr("scrapper_tool.crawl.batch.shutil.which", lambda name: "/x/obscura")
    assert obscura_available() is True
