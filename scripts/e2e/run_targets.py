#!/usr/bin/env python3
"""Run the live target list in ``targets.yaml`` and report what actually works.

**Local only. Never CI.** See the header of ``targets.yaml``: these are real,
commercially-protected sites, and the library must not generate detectable load
on third parties from a build. ``tests/canary_targets.yaml`` is the CI-safe list
and this does not replace it.

Usage
-----

::

    uv run python scripts/e2e/run_targets.py --category cascade
    uv run python scripts/e2e/run_targets.py --category captcha-detect
    uv run python scripts/e2e/run_targets.py --category cookies
    uv run python scripts/e2e/run_targets.py --category captcha-solve --attempts 5
    uv run python scripts/e2e/run_targets.py --category all

Exit code is 0 when every attempted check met its recorded expectation, 1
otherwise. Targets with no expectation, or that need something this machine
lacks (a vision model, a logged-in domain), report SKIP and do not fail the run
— an honest skip is worth more than a green tick that proved nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_TARGETS = _HERE / "targets.yaml"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    marker = {PASS: "ok  ", FAIL: "FAIL", SKIP: "skip"}[status]
    print(f"  [{marker}] {name:26} {detail}", flush=True)


def load_targets() -> dict[str, Any]:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        print("PyYAML is required: uv sync --extra dev", file=sys.stderr)
        raise SystemExit(2) from None
    with _TARGETS.open(encoding="utf-8") as fh:
        return dict(yaml.safe_load(fh))


# --- cascade ---------------------------------------------------------------


async def run_cascade(targets: list[dict[str, Any]]) -> None:
    """Ladder first; render only when the ladder produced no real content."""
    from scrapper_tool._challenge import has_real_content, is_interstitial
    from scrapper_tool.ladder import request_with_ladder
    from scrapper_tool.patterns.render import render_html

    for entry in targets:
        name, url = entry["id"], entry["url"]
        expect = entry.get("expect")
        started = time.perf_counter()
        won = "blocked"
        detail = ""

        try:
            resp, profile = await request_with_ladder("GET", url, timeout=30)
            body = resp.text or ""
            if has_real_content(body, resp.status_code):
                won = "ladder"
                detail = f"ladder {resp.status_code}, {len(body) // 1024} KB ({profile})"
        except Exception as exc:  # ladder exhausted every profile
            detail = f"ladder blocked ({type(exc).__name__})"

        if won != "ladder":
            try:
                rendered = await asyncio.wait_for(render_html(url, timeout_s=60), timeout=90)
                vendor = is_interstitial(rendered.html, rendered.status)
                if has_real_content(rendered.html, rendered.status):
                    won = "render"
                    detail = f"render {rendered.status}, {len(rendered.html) // 1024} KB"
                else:
                    detail = f"render {rendered.status}, {len(rendered.html) // 1024} KB, {vendor}"
            except Exception as exc:
                # An infrastructure failure is NOT an anti-bot wall. Saying
                # "blocked" here understates the pass rate and sends the reader
                # hunting for a bypass that was never needed.
                detail = f"render ERROR {type(exc).__name__} — retry alone before believing this"

        status = PASS if expect is None or won == expect else FAIL
        record(status, name, f"{won:7} {detail}  [{time.perf_counter() - started:.0f}s]")


# --- captcha detection -----------------------------------------------------


async def run_captcha_detect(targets: list[dict[str, Any]]) -> None:
    from camoufox.async_api import AsyncCamoufox

    from scrapper_tool.agent.backends.captcha_dom import detect_challenge_detail

    async with AsyncCamoufox(headless=True, humanize=True) as browser:
        for entry in targets:
            name, url = entry["id"], entry["url"]
            expect = entry.get("expect_kind")
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(6000)
                found = await detect_challenge_detail(page)
                kind = found.kind if found else None
                key = (found.site_key[:24] if found and found.site_key else "-") or "-"
                if kind == expect:
                    record(PASS, name, f"{kind or 'None'}  key={key}")
                else:
                    record(FAIL, name, f"got {kind or 'None'}, expected {expect or 'None'}")
            except Exception as exc:
                record(FAIL, name, f"{type(exc).__name__}: {exc!s:.60}")
            finally:
                await page.close()


# --- captcha solving -------------------------------------------------------


async def _solve_recaptcha(page: Any, vision: Any) -> bool:
    from scrapper_tool.agent.backends.captcha_dom import click_checkbox, read_response_token
    from scrapper_tool.agent.backends.captcha_vision import solve_grid

    if await click_checkbox(page, "recaptcha-v2", settle_s=6):
        return True
    if vision is None:
        return False
    await solve_grid(page, "recaptcha-v2", vision, settle_s=10, max_rounds=3)
    return bool(await read_response_token(page, "recaptcha-v2"))


_GEETEST_SUCCESS_JS = """
() => {
  const el = document.querySelector('.geetest_success_radar_tip_content');
  if (!el) return false;
  const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && s.display !== 'none'
      && s.visibility !== 'hidden' && (el.innerText || '').trim().length > 0;
}
"""


async def _solve_geetest(page: Any) -> bool:
    """Open the puzzle, drag it, and ask GeeTest — not ourselves — whether it took."""
    from scrapper_tool.agent.backends.captcha_slider import solve_slider

    for selector in (".geetest_radar_btn", ".geetest_btn"):
        element = await page.query_selector(selector)
        if element is not None:
            await element.click()
            break
    for _ in range(12):
        await page.wait_for_timeout(1000)
        if await page.evaluate("() => !!document.querySelector('canvas.geetest_canvas_bg')"):
            break
    else:
        return False
    await solve_slider(page, "geetest")
    await page.wait_for_timeout(5000)
    return bool(await page.evaluate(_GEETEST_SUCCESS_JS))


async def run_captcha_solve(targets: list[dict[str, Any]], attempts: int, model: str) -> None:
    from camoufox.async_api import AsyncCamoufox

    from scrapper_tool.agent.backends.llm import OpenAICompatBackend, supports_vision

    base_url = "http://127.0.0.1:6543"
    vision: Any = None
    if await supports_vision(model, base_url):
        vision = OpenAICompatBackend(model=model, base_url=base_url)
    else:
        print(f"  (no vision model at {base_url}; grid targets will SKIP)")

    async with AsyncCamoufox(headless=True, humanize=True) as browser:
        for entry in targets:
            name, url, kind = entry["id"], entry.get("url"), entry.get("kind")
            if not url:
                record(SKIP, name, "no target URL — see note in targets.yaml")
                continue
            if kind in {"recaptcha-v2", "hcaptcha"} and vision is None:
                record(SKIP, name, "needs a local vision model")
                continue

            wins = 0
            for _ in range(attempts):
                page = await browser.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(4000)
                    if kind == "geetest":
                        wins += bool(await _solve_geetest(page))
                    elif kind == "recaptcha-v2":
                        wins += bool(await _solve_recaptcha(page, vision))
                    else:
                        record(SKIP, name, f"no solve path wired for {kind}")
                        break
                except Exception as exc:
                    print(f"    attempt error: {type(exc).__name__}: {exc!s:.70}")
                finally:
                    await page.close()
            else:
                # Solve rates are the point; a bare pass/fail hides everything.
                record(
                    PASS if wins else FAIL,
                    name,
                    f"{wins}/{attempts} accepted by the site",
                )


# --- cookies ---------------------------------------------------------------


async def run_cookies(model: str, profile_dir: Path) -> None:
    """Solve once, harvest, relaunch a SEPARATE browser, prove it inherited."""
    from camoufox.async_api import AsyncCamoufox

    from scrapper_tool.agent.backends.captcha import CamoufoxAutoSolver
    from scrapper_tool.agent.backends.captcha_dom import (
        make_captcha_consumer,
        read_context_cookies,
        read_response_token,
    )
    from scrapper_tool.agent.backends.llm import OpenAICompatBackend, supports_vision

    base_url = "http://127.0.0.1:6543"
    vision: Any = None
    if await supports_vision(model, base_url):
        vision = OpenAICompatBackend(model=model, base_url=base_url)

    url = "https://www.google.com/recaptcha/api2/demo"
    # One-shot local mkdir; the async-filesystem rule is aimed at hot paths.
    profile_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

    async def launch(solve: bool) -> tuple[int, int, int]:
        async with AsyncCamoufox(
            headless=True, humanize=True, persistent_context=True, user_data_dir=str(profile_dir)
        ) as browser:
            pages = getattr(browser, "pages", None)
            page = pages[0] if pages else await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(4000)
            at_start = len(await read_context_cookies(page))
            harvested = 0
            token = 0
            if solve:
                collected: list[dict[str, Any]] = []
                consumer = make_captcha_consumer(
                    CamoufoxAutoSolver(settle_s=2), vision=vision, on_solved=collected.extend
                )
                await _solve_recaptcha(page, vision)
                await consumer(page, url=url)
                harvested = len(collected)
                token = len(await read_response_token(page, "recaptcha-v2"))
            await page.wait_for_timeout(1000)
            return at_start, harvested, token

    start1, harvested, token = await launch(solve=True)
    if token:
        record(PASS, "solve-then-harvest", f"token {token} chars, {harvested} cookie(s) harvested")
    else:
        record(SKIP, "solve-then-harvest", "no solve this run — cannot judge the harvest")

    start2, _, _ = await launch(solve=False)
    if start2 > start1:
        record(
            PASS,
            "clearance-survives-relaunch",
            f"separate browser started with {start2} cookie(s) (run 1 started with {start1})",
        )
    else:
        record(FAIL, "clearance-survives-relaunch", f"run 2 started with {start2}, run 1 {start1}")


# --- entry point -----------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--category",
        default="all",
        choices=["all", "cascade", "captcha-detect", "captcha-solve", "cookies"],
    )
    parser.add_argument("--attempts", type=int, default=4, help="tries per solve target")
    parser.add_argument("--model", default="qwen/qwen3.8-27b")
    parser.add_argument("--profile-dir", type=Path, default=Path("./.e2e-profile"))
    args = parser.parse_args()

    data = load_targets()
    want = args.category

    if want in ("all", "cascade"):
        print("\n=== cascade (ladder -> render) ===")
        await run_cascade(data.get("cascade", []))
    if want in ("all", "captcha-detect"):
        print("\n=== captcha detection ===")
        await run_captcha_detect(data.get("captcha_detect", []))
    if want in ("all", "captcha-solve"):
        print("\n=== captcha solving ===")
        await run_captcha_solve(data.get("captcha_solve", []), args.attempts, args.model)
    if want in ("all", "cookies"):
        print("\n=== cookie transfer ===")
        await run_cookies(args.model, args.profile_dir)

    failed = [r for r in _results if r[0] == FAIL]
    skipped = [r for r in _results if r[0] == SKIP]
    passed = [r for r in _results if r[0] == PASS]
    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    for _, name, detail in failed:
        print(f"  FAILED  {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
