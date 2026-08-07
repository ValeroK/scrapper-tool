"""``scrapper-tool doctor`` — preflight every cascade tier.

``/ready`` answers *"can this server serve requests?"*. Doctor answers a
different question: *"which cascade tiers are actually functional on this
machine?"* — and, when one isn't, what single command fixes it.

That distinction matters because this install has several ways to be
half-working, each of which fails late and opaquely:

* ``import camoufox`` succeeds while the Firefox blob is absent, because the
  Dockerfile runs ``camoufox fetch || true``. The module probe says ``ok`` and
  the first render 500s.
* Camoufox 0.4+ refuses to launch at all without ``geoip2``.
* **E2 cannot run on the default configuration.** The default backend is
  ``camoufox``, Firefox has no CDP, and ``agent_browse`` hard-raises for any
  backend without a CDP endpoint. Nothing tells you until a request escalates
  that far.

Exit codes
----------

- ``0`` — ready: every tier is ``ok`` or deliberately disabled.
- ``1`` — degraded: the cheap A/B/C path works, something above it doesn't.
- ``2`` — not ready: even A/B/C is broken, doctor itself errored, or a
  ``--require-tier`` gate was not met.

``--require-tier <name>`` turns doctor into a CI / container healthcheck gate:
exit 0 only when that specific tier is ``ok``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING, Any

from scrapper_tool import __version__, _extras
from scrapper_tool.recipe.policy import TIER_ORDER

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence

__all__ = ["add_subparser", "main", "run_cli", "run_doctor"]

#: Order rows appear in the report. ``TIER_ORDER`` is the cascade's own tier
#: naming (``a_b_c``, ``d``, ``render``, ``e1``, ``e2``) — reused verbatim so
#: doctor's row labels match ``pattern_used`` and ``escalation_log`` exactly.
#: ``replay`` sits in front of the cascade and ``cookies`` cuts across it, so
#: both are appended rather than invented mid-list.
REPORT_TIERS: tuple[str, ...] = ("replay", *TIER_ORDER, "cookies")

#: How long a single network-touching probe may take before doctor gives up on
#: it. Doctor is a diagnostic — a hung probe is a failed diagnosis.
_PROBE_TIMEOUT_S = 5.0

_STATUS_READY = "ready"
_STATUS_DEGRADED = "degraded"
_STATUS_NOT_READY = "not_ready"

#: Tier states that don't count against overall health. ``disabled`` is an
#: operator's explicit choice, not a fault.
_OK_STATES = frozenset({"ok", "disabled"})


class _TierResult:
    """One row of the report: a status, a human detail, and any fix commands."""

    __slots__ = ("detail", "fixes", "status")

    def __init__(self, status: str, detail: str, fixes: Sequence[str] = ()) -> None:
        self.status = status
        self.detail = detail
        self.fixes = list(fixes)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "fixes": self.fixes}


def _hint(key: str) -> str:
    """Look up an install hint, falling back to the key so a typo is visible."""
    return _extras.INSTALL_HINTS.get(key, key)


# ---------------------------------------------------------------------------
# Individual tier probes
# ---------------------------------------------------------------------------


def _probe_replay() -> _TierResult:
    """The recipe cache sits in front of the cascade; a read-only dir silently disables it."""
    from scrapper_tool.recipe.store import default_cache_dir, recipe_cache_enabled  # noqa: PLC0415

    if not recipe_cache_enabled():
        return _TierResult("disabled", "SCRAPPER_TOOL_RECIPE_CACHE=0")

    cache_dir = default_cache_dir()
    probe = cache_dir / ".doctor-write-probe"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        return _TierResult("degraded", f"cache dir not writable at {cache_dir}: {exc}")
    return _TierResult("ok", f"cache dir writable at {cache_dir}")


def _probe_a_b_c() -> _TierResult:
    """A/B/C rides the core dependencies only, so it is available in every install."""
    from scrapper_tool.ladder import IMPERSONATE_LADDER  # noqa: PLC0415

    return _TierResult("ok", ", ".join(IMPERSONATE_LADDER))


def _probe_d() -> _TierResult:
    if not _extras.hostile_available():
        return _TierResult("missing", "[hostile] not installed", [_hint("hostile")])
    if not _extras.browser_binary_present("scrapling"):
        return _TierResult(
            "degraded",
            "scrapling installed, but no Camoufox/Firefox binary found on disk",
            [_hint("playwright-firefox")],
        )
    return _TierResult("ok", "scrapling + browser binary present")


def _probe_render(cfg: Any) -> _TierResult:
    if not _extras.render_tier_enabled():
        return _TierResult("disabled", "SCRAPPER_TOOL_RENDER_TIER=0")
    if cfg is None:
        return _TierResult("missing", "[llm-agent] not installed", [_hint("llm-agent")])

    module_state = _extras.check_browser_module(cfg.browser)
    if module_state != "ok":
        return _TierResult(
            "missing",
            f"backend module for {cfg.browser!r} is {module_state}",
            [_hint("llm-agent")],
        )
    if not _extras.browser_binary_present(cfg.browser):
        fix = {
            "camoufox": _hint("camoufox-binary"),
            "patchright": _hint("patchright-chromium"),
        }.get(cfg.browser, _hint("playwright-firefox"))
        return _TierResult(
            "degraded",
            f"{cfg.browser} module imports, but its browser binary is missing",
            [fix],
        )
    return _TierResult("ok", f"{cfg.browser} module + binary present")


async def _probe_e1(cfg: Any) -> _TierResult:  # noqa: PLR0911 — one return per failure mode
    if not _extras.crawl4ai_available():
        return _TierResult("missing", "crawl4ai not installed", [_hint("llm-agent")])
    if cfg is None:
        return _TierResult("missing", "[llm-agent] not installed", [_hint("llm-agent")])

    try:
        reachable, model_available = await asyncio.wait_for(
            _extras.probe_llm(cfg), timeout=_PROBE_TIMEOUT_S
        )
    except TimeoutError:
        return _TierResult(
            "degraded",
            f"crawl4ai ok; LLM probe timed out after {_PROBE_TIMEOUT_S:g}s at {cfg.ollama_url}",
            [f"ollama serve && ollama pull {cfg.model}"],
        )

    if reachable is None:
        return _TierResult("ok", f"crawl4ai ok; {cfg.llm} backend is not probeable")
    if not reachable:
        return _TierResult(
            "degraded",
            f"crawl4ai ok; LLM unreachable at {cfg.ollama_url}",
            [f"ollama serve && ollama pull {cfg.model}"],
        )
    if not model_available:
        return _TierResult(
            "degraded",
            f"LLM reachable at {cfg.ollama_url}; model {cfg.model!r} not available",
            [f"ollama pull {cfg.model}"],
        )
    return _TierResult("ok", f"crawl4ai + {cfg.llm} serving {cfg.model}")


def _probe_e2(cfg: Any) -> _TierResult:
    """E2 needs a CDP endpoint. On the *default* backend there isn't one.

    browser-use 0.13 attaches over CDP only, and ``agent_browse`` refuses to run
    without a CDP URL rather than let browser-use silently build a fresh
    unpatched Chromium. Camoufox is Firefox, and Firefox dropped CDP — so a
    stock config can never run E2.
    """
    if cfg is None:
        return _TierResult("missing", "[llm-agent] not installed", [_hint("llm-agent")])

    if cfg.browser == "camoufox":
        return _TierResult(
            "blocked",
            "browser=camoufox exposes no CDP endpoint (Firefox dropped CDP)",
            ["set SCRAPPER_TOOL_AGENT_BROWSER=patchright to enable E2"],
        )
    if cfg.browser == "obscura":
        if not _extras.obscura_endpoint_reachable():
            url = os.environ.get("SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL", "http://127.0.0.1:9222")
            return _TierResult(
                "degraded",
                f"obscura CDP endpoint unreachable at {url}",
                ["start the Obscura sidecar (docker compose up obscura)"],
            )
        return _TierResult("ok", "obscura CDP endpoint reachable")
    if not _extras.browser_binary_present(cfg.browser):
        return _TierResult(
            "degraded",
            f"{cfg.browser} exposes CDP, but its browser binary is missing",
            [_hint("patchright-chromium")],
        )
    return _TierResult("ok", f"{cfg.browser} exposes a CDP endpoint")


def _probe_cookies() -> _TierResult:
    """Cookie extraction is host-side and platform-sensitive; say so precisely."""
    if not _extras.cookie_backend_available():
        return _TierResult("missing", "rookiepy not installed", [_hint("cookies")])

    platform_note = {
        "darwin": "macOS: Chrome's key is in the login Keychain — the first read prompts",
        "win32": "Windows: Chrome 127+ App-Bound Encryption may need admin; Firefox is reliable",
    }.get(sys.platform, "Linux: Chrome needs the Secret Service; Firefox reads unencrypted")
    return _TierResult("ok", f"cookie backend present. {platform_note}")


# ---------------------------------------------------------------------------
# Environment-level checks (not tiers)
# ---------------------------------------------------------------------------


def _environment_checks(cfg: Any) -> tuple[dict[str, Any], list[str]]:
    """Cross-cutting facts that don't belong to a single tier."""
    checks: dict[str, Any] = {}
    fixes: list[str] = []

    checks["python"] = f"{sys.version_info.major}.{sys.version_info.minor}"
    checks["platform"] = sys.platform
    checks["browser"] = getattr(cfg, "browser", None)

    # Camoufox 0.4+ raises NotInstalledGeoIPExtra at launch without geoip2. Only
    # worth reporting when Camoufox is the backend actually in use.
    geoip_ok = _extras.geoip2_available()
    checks["geoip2"] = geoip_ok
    if cfg is not None and cfg.browser == "camoufox" and not geoip_ok:
        fixes.append(_hint("geoip2"))

    # The Dockerfile finding: `camoufox fetch || true` means a build-time blip
    # ships an image where the module imports but the blob is absent. Report
    # module and binary separately — knowing *which* half is missing is the
    # whole value.
    if cfg is not None:
        checks["browser_module"] = _extras.check_browser_module(cfg.browser)
        checks["browser_binary"] = _extras.browser_binary_present(cfg.browser)

    checks["user_data_dir_supported"] = _extras.user_data_dir_supported()

    # Whether caller-supplied cookies can actually reach the LLM tiers — the
    # difference between "cookies were applied" and a silently logged-out page.
    #
    # The two tiers are asked different questions on purpose, because they carry
    # a session by different mechanisms:
    #
    # * E1 lets Crawl4AI launch its own browser, so the session rides on
    #   ``BrowserConfig(cookies=...)`` and the probe is "does this build declare
    #   that parameter".
    # * E2 attaches over CDP to a browser we already launched and sets cookies on
    #   the live context, so no browser-use kwarg is involved at all. What
    #   decides it is whether the configured backend exposes a CDP endpoint —
    #   Camoufox is Firefox, and Firefox dropped CDP. An earlier version of this
    #   probe reported ``e2_accepts_storage_state`` from a browser-use signature;
    #   that kwarg is not on E2's path, so it answered True for a route that did
    #   not exist.
    if _extras.crawl4ai_available():
        checks["e1_accepts_cookies"] = _extras.crawl4ai_accepts("cookies")
    if _extras.agent_available() and cfg is not None:
        checks["e2_accepts_cookies"] = cfg.browser != "camoufox"

    checks["captcha_key"] = "set" if os.environ.get("SCRAPPER_TOOL_CAPTCHA_KEY") else "not set"

    checks["proxy_pool"] = _proxy_pool_state()

    lxml_state = _lxml_state()
    if lxml_state is not None:
        checks["lxml"] = lxml_state

    return checks, fixes


def _proxy_pool_state() -> str:
    """``none`` / ``trusted (n)`` / ``UNTRUSTED (n)``.

    An untrusted pool is not an error — it's fine for anonymous scraping — but
    it must never carry credentials, so doctor names it loudly.
    """
    try:
        from scrapper_tool.proxy import ProxyPool  # noqa: PLC0415

        pool = ProxyPool.from_env()
    except Exception:
        return "unknown"
    if pool is None:
        return "none"
    label = "UNTRUSTED" if pool.untrusted else "trusted"
    return f"{label} ({len(pool.entries)})"


def _lxml_state() -> str | None:
    """Report the resolved lxml when Scrapling and Crawl4AI co-exist.

    Both declare conservative, mutually incompatible lxml pins; the ``[full]``
    install only resolves because ``[tool.uv]`` overrides them. If someone
    installs without uv, this is the line that explains the breakage.
    """
    if not (_extras.hostile_available() and _extras.crawl4ai_available()):
        return None
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("lxml")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _load_agent_config() -> tuple[Any, str | None]:
    """Return ``(cfg, error)``. ``cfg`` is None when the extra isn't installed."""
    if not _extras.agent_available():
        return None, None
    try:
        from scrapper_tool.agent.types import AgentConfig  # noqa: PLC0415

        return AgentConfig.from_env(), None
    except Exception as exc:
        return None, str(exc)


async def run_doctor(*, require_tier: str | None = None) -> dict[str, Any]:
    """Probe every tier and return the report as plain data.

    Pure in the sense that matters: it returns a dict (``exit_code`` included as
    data, following ``run_canary``'s contract) and writes nothing to stdout, so
    both the text and JSON renderers — and any future MCP wrapper — consume the
    same structure.
    """
    cfg, cfg_error = _load_agent_config()

    tiers: dict[str, _TierResult] = {
        "replay": _probe_replay(),
        "a_b_c": _probe_a_b_c(),
        "d": _probe_d(),
        "render": _probe_render(cfg),
        "e1": await _probe_e1(cfg),
        "e2": _probe_e2(cfg),
        "cookies": _probe_cookies(),
    }

    checks, env_fixes = _environment_checks(cfg)
    if cfg_error is not None:
        checks["agent_config_error"] = cfg_error

    # De-duplicate fixes while preserving first-seen order: the same
    # `pip install` line is often the remedy for several tiers at once, and a
    # Fixes block that repeats itself reads as noise.
    fixes: list[str] = []
    for tier_name in REPORT_TIERS:
        for fix in tiers[tier_name].fixes:
            if fix not in fixes:
                fixes.append(fix)
    for fix in env_fixes:
        if fix not in fixes:
            fixes.append(fix)

    status, exit_code = _resolve_status(tiers, require_tier=require_tier)

    return {
        "status": status,
        "version": __version__,
        "exit_code": exit_code,
        "require_tier": require_tier,
        "tiers": {name: tiers[name].as_dict() for name in REPORT_TIERS},
        "checks": checks,
        "fixes": fixes,
    }


def _resolve_status(tiers: dict[str, _TierResult], *, require_tier: str | None) -> tuple[str, int]:
    """Map tier states to an overall status and exit code."""
    if require_tier is not None:
        result = tiers.get(require_tier)
        if result is None:
            return _STATUS_NOT_READY, 2
        if result.status == "ok":
            return _STATUS_READY, 0
        return _STATUS_NOT_READY, 2

    # A/B/C is the floor. If the core HTTP path is broken, nothing above it
    # matters and the install is not merely degraded.
    if tiers["a_b_c"].status not in _OK_STATES:
        return _STATUS_NOT_READY, 2

    if all(result.status in _OK_STATES for result in tiers.values()):
        return _STATUS_READY, 0
    return _STATUS_DEGRADED, 1


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"scrapper-tool doctor — v{report['version']}     Status: {report['status']}")
    lines.append("")
    lines.append(f"{'Tier':<8} | {'Status':<8} | Detail")
    lines.append(f"{'-' * 8} | {'-' * 8} | {'-' * 48}")

    tiers = report["tiers"]
    for name in REPORT_TIERS:
        row = tiers[name]
        lines.append(f"{name:<8} | {row['status']:<8} | {row['detail']}")

    checks = report.get("checks") or {}
    if checks:
        lines.append("")
        lines.append("Environment:")
        for key, value in checks.items():
            lines.append(f"  {key}: {value}")

    fixes = report.get("fixes") or []
    if fixes:
        lines.append("")
        lines.append("Fixes:")
        lines.extend(f"  {fix}" for fix in fixes)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def add_subparser(sub: Any) -> None:
    """Register the ``doctor`` subcommand on ``scrapper-tool``'s dispatcher."""
    doctor = sub.add_parser(
        "doctor",
        help="Preflight every cascade tier and report what's broken plus how to fix it.",
        description=(
            "Probes each cascade tier (replay, A/B/C, D, render, E1, E2, cookies) "
            "and reports which are functional. Exit 0 ready, 1 degraded, 2 not "
            "ready. Use --require-tier to gate a healthcheck on one specific tier."
        ),
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text table.",
    )
    doctor.add_argument(
        "--require-tier",
        type=str,
        default=None,
        metavar="NAME",
        help=("Exit 0 only if this tier is 'ok'. One of: " + ", ".join(REPORT_TIERS) + "."),
    )


def run_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Handler invoked by the ``scrapper-tool`` dispatcher."""
    require_tier: str | None = getattr(args, "require_tier", None)
    if require_tier is not None and require_tier not in REPORT_TIERS:
        parser.error(f"--require-tier must be one of: {', '.join(REPORT_TIERS)}")

    try:
        report = asyncio.run(run_doctor(require_tier=require_tier))
    except Exception as exc:  # pragma: no cover — defensive; probes swallow their own
        sys.stderr.write(f"doctor error: {exc}\n")
        return 2

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2))
    else:
        sys.stdout.write(_format_text(report))
    sys.stdout.write("\n")

    exit_code = report["exit_code"]
    assert isinstance(exit_code, int)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Direct entry point — ``python -m scrapper_tool.doctor [flags]``.

    ``argv`` omits the ``doctor`` word, since the module *is* the subcommand.
    When it is None we read ``sys.argv[1:]`` explicitly rather than defaulting
    to an empty list: argparse's own None-means-sys.argv convention doesn't
    apply here because we are *constructing* an argv for the dispatcher, and
    building ``["doctor"]`` would silently discard every flag the user typed.
    """
    from scrapper_tool.cli import main as _cli_main  # noqa: PLC0415

    forwarded = sys.argv[1:] if argv is None else list(argv)
    return _cli_main(["doctor", *forwarded])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
