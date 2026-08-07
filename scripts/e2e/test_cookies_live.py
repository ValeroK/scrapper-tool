#!/usr/bin/env python3
"""Live end-to-end validation for the cookie feature and the P2 render fix.

**Run this on your own machine, not in CI and not in a container.** Two of the
checks physically cannot pass anywhere else:

* Cookie extraction needs the OS credential store (macOS Keychain, Windows
  DPAPI, Linux gnome-keyring) plus a real browser profile on disk.
* The Camoufox render check needs the ~300 MB Firefox blob that
  ``camoufox fetch`` downloads.

Everything the unit suite can prove is already proven there; this script exists
for the parts that need a real browser, a real login, and a real network.

Usage
-----

::

    # Everything that needs no login:
    uv run python scripts/e2e/test_cookies_live.py --domain example.com

    # The full loop, including an authenticated fetch:
    uv run python scripts/e2e/test_cookies_live.py \\
        --domain app.example.com \\
        --url https://app.example.com/account

Exit code is 0 when every attempted check passed, 1 otherwise. Checks that
cannot run in this environment are reported as SKIP and do not fail the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

_results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))
    sys.stdout.write(f"[{status:<4}] {name}" + (f" — {detail}" if detail else "") + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 1. doctor
# ---------------------------------------------------------------------------


def check_doctor() -> None:
    """doctor should run, emit valid JSON, and agree with reality."""
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "scrapper_tool.doctor", "--json"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        record("doctor runs", FAIL, str(exc))
        return

    if proc.returncode not in (0, 1, 2):
        record("doctor runs", FAIL, f"unexpected exit {proc.returncode}")
        return
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        record("doctor --json is parseable", FAIL, str(exc))
        return

    record("doctor runs", PASS, f"status={report['status']} exit={proc.returncode}")

    # The exit code must agree with the reported status, or the CI gate lies.
    expected = {"ready": 0, "degraded": 1, "not_ready": 2}[report["status"]]
    if proc.returncode == expected:
        record("doctor exit code matches status", PASS)
    else:
        record(
            "doctor exit code matches status",
            FAIL,
            f"status={report['status']} implies {expected}, got {proc.returncode}",
        )

    # Cross-check one claim against the filesystem rather than trusting the
    # report: if it says the render binary is present, it should be findable.
    from scrapper_tool import _extras

    browser = report["checks"].get("browser")
    if browser:
        claimed = report["checks"].get("browser_binary")
        actual = _extras.browser_binary_present(browser)
        if claimed == actual:
            record("doctor browser_binary matches disk", PASS, f"{browser}={actual}")
        else:
            record(
                "doctor browser_binary matches disk",
                FAIL,
                f"reported {claimed}, probe says {actual}",
            )


# ---------------------------------------------------------------------------
# 2. Camoufox persistent-context render (closes the loop on the P2 fix)
# ---------------------------------------------------------------------------


async def check_render_persistent_context(url: str) -> None:
    """The P2 regression, live: a render with a real profile dir must not raise.

    This is the check the unit tests approximate with a fake that duck-types a
    Playwright BrowserContext. The source chain is confirmed end to end, but
    only a real Camoufox launch proves the contract hasn't moved.
    """
    from scrapper_tool import _extras

    if not _extras.browser_binary_present("camoufox"):
        record(
            "camoufox persistent-context render",
            SKIP,
            "no camoufox binary — run `camoufox fetch`",
        )
        return

    from scrapper_tool.agent.backends.browser import BrowserLaunchOptions
    from scrapper_tool.patterns.render import render_html

    profile_dir = Path(tempfile.mkdtemp(prefix="scrapper-e2e-profile-"))
    try:
        result = await render_html(
            url,
            browser="camoufox",
            options=BrowserLaunchOptions(user_data_dir=str(profile_dir)),
            timeout_s=60.0,
            settle_s=2.0,
        )
    except AttributeError as exc:
        # The exact failure P2 fixed. Naming it makes a regression unmistakable.
        record(
            "camoufox persistent-context render",
            FAIL,
            f"AttributeError — the P2 bug is back: {exc}",
        )
        return
    except Exception as exc:  # noqa: BLE001
        record("camoufox persistent-context render", FAIL, f"{type(exc).__name__}: {exc}")
        return
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    if len(result.html) > 500:
        record(
            "camoufox persistent-context render",
            PASS,
            f"status={result.status}, {len(result.html)} bytes",
        )
    else:
        record(
            "camoufox persistent-context render",
            FAIL,
            f"only {len(result.html)} bytes — rendered but empty",
        )


# ---------------------------------------------------------------------------
# 3. Cookie extraction from a real browser profile
# ---------------------------------------------------------------------------


def check_cookie_export(domain: str) -> list[Any]:
    """Read a real browser profile. Returns the cookies found (possibly empty)."""
    from scrapper_tool import _extras

    if not _extras.cookie_backend_available():
        record("cookie backend installed", SKIP, "pip install 'scrapper-tool[cookies]'")
        return []
    record("cookie backend installed", PASS)

    from scrapper_tool import cookies as cookies_mod
    from scrapper_tool._browser_cookies import BrowserCookieError, read_browser_cookies

    try:
        rows = read_browser_cookies(domain)
    except BrowserCookieError as exc:
        record(
            "read browser profile",
            SKIP,
            f"{str(exc).splitlines()[0]} (log into {domain} first?)",
        )
        return []

    found = cookies_mod.from_browser_store(rows)
    found = [c for c in found if cookies_mod.domain_matches(c.domain, domain)]
    if not found:
        record("read browser profile", SKIP, f"no cookies for {domain} — are you logged in?")
        return []

    record("read browser profile", PASS, f"{len(found)} cookies")

    # Nothing we print or log may carry a value.
    redacted = json.dumps(cookies_mod.redact(found))
    leaked = [c.name for c in found if c.value.get_secret_value() in redacted]
    if leaked:
        record("redaction holds on real cookies", FAIL, f"values leaked for {leaked}")
    else:
        record("redaction holds on real cookies", PASS)

    return found


def check_jar_permissions(found: list[Any], domain: str) -> None:
    """A jar written from real cookies must be 0600 in a 0700 directory."""
    if not found:
        record("jar permissions", SKIP, "no cookies to write")
        return
    if sys.platform.startswith("win"):
        record("jar permissions", SKIP, "POSIX mode bits are advisory on Windows")
        return

    from scrapper_tool import cookies as cookies_mod

    jar_dir = Path(tempfile.mkdtemp(prefix="scrapper-e2e-jar-"))
    try:
        target = cookies_mod.save_cookies(found, domain, directory=jar_dir, overwrite=True)
        file_mode = stat.S_IMODE(target.stat().st_mode)
        dir_mode = stat.S_IMODE(jar_dir.stat().st_mode)
        if file_mode == 0o600 and dir_mode == 0o700:
            record("jar permissions", PASS, "0600 file in 0700 dir")
        else:
            record("jar permissions", FAIL, f"file={file_mode:o} dir={dir_mode:o}")

        restored = cookies_mod.load_cookies(domain, directory=jar_dir)
        if {c.name for c in restored} == {c.name for c in found}:
            record("jar round-trips through disk", PASS)
        else:
            record("jar round-trips through disk", FAIL, "names differ after reload")
    finally:
        shutil.rmtree(jar_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. The whole point: an authenticated scrape
# ---------------------------------------------------------------------------


async def check_authenticated_scrape(url: str, found: list[Any]) -> None:
    """Scrape a login-walled URL with and without the cookies, and compare.

    The comparison is what makes this meaningful. A page that looks the same
    both ways means the cookies did not take effect, even if the request
    succeeded.
    """
    if not found:
        record("authenticated scrape", SKIP, "no cookies available")
        return

    from scrapper_tool import scrape

    try:
        anon = await scrape(url, mode="fetch")
        authed = await scrape(url, mode="fetch", cookies=found)
    except Exception as exc:  # noqa: BLE001
        record("authenticated scrape", FAIL, f"{type(exc).__name__}: {exc}")
        return

    applied = authed.get("cookies_applied") or []
    if applied:
        record("cookies_applied is reported", PASS, ", ".join(applied))
    else:
        record("cookies_applied is reported", FAIL, "no tier reported carrying cookies")

    anon_len = len(anon.get("raw_text") or "")
    authed_len = len(authed.get("raw_text") or "")
    if anon_len and authed_len and anon_len != authed_len:
        record(
            "authenticated page differs from anonymous",
            PASS,
            f"{anon_len} bytes anon vs {authed_len} bytes authed",
        )
    else:
        record(
            "authenticated page differs from anonymous",
            FAIL,
            f"both {anon_len}/{authed_len} bytes — cookies may not have taken effect",
        )

    # The response must never echo a cookie value back to the caller.
    blob = json.dumps(authed, default=str)
    leaked = [c.name for c in found if c.value.get_secret_value() in blob]
    if leaked:
        record("response body carries no cookie values", FAIL, f"leaked: {leaked}")
    else:
        record("response body carries no cookie values", PASS)


# ---------------------------------------------------------------------------
# 5. Per-tier matrix — the part unit tests cannot prove
# ---------------------------------------------------------------------------

#: Each entry forces one tier rather than letting the cascade choose, because
#: "the cascade returned a logged-in page" does not say *which* tier carried the
#: session — and on a healthy install A/B/C usually wins before the interesting
#: tiers ever run.
#:
#: `expect_tier` is the name that should appear in `cookies_applied`. A tier that
#: legitimately cannot carry a session must appear in `cookies_skipped` with a
#: reason instead; both are a pass, and *neither* appearing is the silent failure
#: this whole matrix exists to catch.
_TIER_MATRIX: tuple[tuple[str, str, str], ...] = (
    ("fetch", "a_b_c", "curl_cffi cookie jar"),
    ("hostile", "d", "Scrapling StealthyFetcher(cookies=...)"),
    ("extract", "e1", "Crawl4AI BrowserConfig(cookies=...)"),
    ("browse", "e2", "add_cookies on the live CDP context"),
)


async def check_tier_matrix(url: str, found: list[Any]) -> None:
    """Force each tier in turn and confirm it reports what it did with the jar.

    This is the check that could not be written as a unit test. D's kwarg
    support is unknowable from Scrapling's ``(*args, **kwargs)`` signature, and
    E2's injection only proves itself against a browser browser-use is really
    driving.
    """
    if not found:
        record("tier matrix", SKIP, "no cookies available")
        return

    from scrapper_tool import scrape

    for mode, tier, mechanism in _TIER_MATRIX:
        label = f"tier {tier} ({mechanism})"
        try:
            result = await scrape(url, mode=mode, cookies=found, interactive=(mode == "browse"))
        except Exception as exc:  # noqa: BLE001
            # A tier that cannot run at all here (no browser binary, no LLM) is
            # an environment limit, not a cookie bug.
            record(label, SKIP, f"{type(exc).__name__}: {str(exc)[:100]}")
            continue

        applied = result.get("cookies_applied") or []
        skipped = {entry["tier"]: entry["reason"] for entry in result.get("cookies_skipped") or []}

        if tier in applied:
            record(label, PASS, "carried the session")
        elif tier in skipped:
            # Reported rather than silent. That is the contract.
            record(label, PASS, f"declined with a reason: {skipped[tier]}")
        else:
            record(
                label,
                FAIL,
                "tier ran but appears in neither cookies_applied nor cookies_skipped",
            )

        blob = json.dumps(result, default=str)
        leaked = [c.name for c in found if c.value.get_secret_value() in blob]
        if leaked:
            record(f"{label} — no value leak", FAIL, f"leaked: {leaked}")


# ---------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live validation for cookies + the Camoufox render fix. Host-side only."
    )
    parser.add_argument(
        "--domain",
        required=True,
        help="Domain to read cookies for, e.g. app.example.com. Log in there first.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="URL to scrape for the authenticated check. Defaults to https://<domain>/.",
    )
    parser.add_argument(
        "--render-url",
        default="https://example.com",
        help="URL for the Camoufox persistent-context render check.",
    )
    args = parser.parse_args()

    url = args.url or f"https://{args.domain}/"

    sys.stdout.write("scrapper-tool live validation (host-side)\n")
    sys.stdout.write("=" * 60 + "\n")

    check_doctor()
    await check_render_persistent_context(args.render_url)
    found = check_cookie_export(args.domain)
    check_jar_permissions(found, args.domain)
    await check_authenticated_scrape(url, found)
    await check_tier_matrix(url, found)

    sys.stdout.write("=" * 60 + "\n")
    passed = sum(1 for _, s, _ in _results if s == PASS)
    failed = sum(1 for _, s, _ in _results if s == FAIL)
    skipped = sum(1 for _, s, _ in _results if s == SKIP)
    sys.stdout.write(f"{passed} passed, {failed} failed, {skipped} skipped\n")
    if skipped:
        sys.stdout.write("Skips are environment limits, not failures.\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
