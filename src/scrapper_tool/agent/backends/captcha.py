"""CAPTCHA solver cascade for Pattern E.

Default = ``auto`` cascade through 2 free OSS tiers, escalating to a
paid solver only when ``captcha_api_key`` is configured:

| Tier | Solver | Solves | Cost | License |
|------|--------|--------|------|---------|
| 0 | CamoufoxAutoSolver | Most CF Turnstile interstitials | $0 | MPL-2.0 |
| 1 | TheykaSolver | CF Turnstile | $0 | MIT |
| 2 | CapSolverSolver (paid) | hCaptcha/reCAPTCHA/Funcaptcha/AWS WAF/DataDome | proprietary |
| 2 | NopechaSolver (paid) | reCAPTCHA / hCaptcha subset | proprietary |
| 2 | TwoCaptchaSolver (paid) | Broadest incl. complex image | proprietary |

There is **no** OSS solver in 2026 that matches CapSolver's coverage of
hCaptcha / reCAPTCHA v3 / Funcaptcha / DataDome. This cascade gives free
coverage for CF Turnstile (the most common 2026 challenge) and falls
through to paid only when an API key is set.

FlareSolverr and Buster are documented in ``docs/patterns/e-llm-agent.md``
as user-runnable adjuncts but are NOT part of the initial implementation.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

from scrapper_tool._logging import get_logger
from scrapper_tool.errors import CaptchaSolveError

if TYPE_CHECKING:
    from scrapper_tool.agent.types import AgentConfig

_logger = get_logger(__name__)


CaptchaKind = Literal[
    "turnstile",
    "hcaptcha",
    "recaptcha-v2",
    "recaptcha-v3",
    "image",
    "funcaptcha",
    "arkose",
    "geetest",
    "aws-waf",
    "datadome",
]


class CaptchaSolver(Protocol):
    """Protocol implemented by every solver tier."""

    name: str
    requires_api_key: bool

    @property
    def supported(self) -> frozenset[CaptchaKind]: ...

    async def solve(
        self,
        kind: CaptchaKind,
        site_key: str,
        url: str,
        *,
        action: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        """Return a token that, when injected into the page, satisfies the
        challenge. Raises :class:`CaptchaSolveError` on failure.
        """


# --- Tier 0 — Camoufox auto-pass (no-op) ---------------------------------


class CamoufoxAutoSolver:
    """Tier 0 — relies on Camoufox passing the challenge silently.

    Returns an empty token; the caller waits up to ``settle_s`` seconds
    after navigation for the challenge to clear before moving on.
    Useful only inside a Camoufox session — using it with another
    backend is a no-op.

    The empty-token return is the "I didn't solve, just waited" signal.
    """

    name = "camoufox-auto"
    requires_api_key = False

    def __init__(self, *, settle_s: float = 8.0) -> None:
        self._settle_s = settle_s

    @property
    def supported(self) -> frozenset[CaptchaKind]:
        return frozenset({"turnstile"})

    async def solve(
        self,
        kind: CaptchaKind,
        site_key: str,
        url: str,
        *,
        action: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        _ = (kind, site_key, url, action, extra)
        await asyncio.sleep(self._settle_s)
        return ""  # empty token = "no explicit solve, settle and reload"


# --- Tier 1 — Theyka/Turnstile-Solver (free OSS) -------------------------


_THEYKA_NOT_INSTALLED = (
    "Theyka Turnstile-Solver requires the [turnstile-solver] extra.\n"
    "Install with: pip install scrapper-tool[llm-agent,turnstile-solver]"
)


class TheykaSolver:
    """Tier 1 — wrap Theyka/Turnstile-Solver.

    Lazy-imports the package; when missing, ``solve`` raises a helpful
    :class:`CaptchaSolveError` so the cascade can move to the next tier
    instead of crashing.
    """

    name = "theyka"
    requires_api_key = False

    @property
    def supported(self) -> frozenset[CaptchaKind]:
        return frozenset({"turnstile"})

    async def solve(
        self,
        kind: CaptchaKind,
        site_key: str,
        url: str,
        *,
        action: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        if kind != "turnstile":
            msg = f"TheykaSolver only handles 'turnstile', got {kind!r}"
            raise CaptchaSolveError(msg)
        try:
            # API surface varies — try a couple of common entry points.
            from turnstile_solver import (  # noqa: PLC0415
                Solver,
            )
        except ImportError as exc:
            raise CaptchaSolveError(_THEYKA_NOT_INSTALLED) from exc

        _ = (action, extra)
        try:
            solver = Solver()
            token = await solver.solve(url=url, sitekey=site_key)
        except Exception as exc:
            msg = f"Theyka solve failed: {exc}"
            raise CaptchaSolveError(msg) from exc
        if not token:
            raise CaptchaSolveError("Theyka returned empty token")
        return str(token)


# --- Tier 2 — paid solvers (CapSolver / NopeCHA / 2Captcha) --------------


class _PaidSolverBase:
    """Shared scaffolding for HTTP paid solvers.

    All three (CapSolver, NopeCHA, 2Captcha) speak HTTP JSON with very
    similar create-task / poll-result patterns. This base does the
    polling loop and timeout enforcement; subclasses fill in the
    endpoints and payload shapes.
    """

    name = "_paid_base"
    requires_api_key = True
    _create_endpoint: str = ""
    _result_endpoint: str = ""

    @property
    def supported(self) -> frozenset[CaptchaKind]:  # pragma: no cover — overridden
        return frozenset()

    def __init__(self, *, api_key: str, timeout_s: float = 120.0) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s

    async def _post_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        resp = await client.post(url, json=payload, timeout=15.0)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            msg = f"{self.name}: non-object response from {url}"
            raise CaptchaSolveError(msg)
        return body


class CapSolverSolver(_PaidSolverBase):
    """Tier 2 — CapSolver. Best 2026 coverage, AI-only, 3-9 s, $0.80-$3/1k."""

    name = "capsolver"
    _api_base = "https://api.capsolver.com"

    @property
    def supported(self) -> frozenset[CaptchaKind]:
        return frozenset(
            {
                "turnstile",
                "hcaptcha",
                "recaptcha-v2",
                "recaptcha-v3",
                "image",
                "funcaptcha",
                "arkose",
                "geetest",
                "aws-waf",
                "datadome",
            }
        )

    async def solve(
        self,
        kind: CaptchaKind,
        site_key: str,
        url: str,
        *,
        action: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        task_type = _capsolver_task_type(kind)
        task_payload: dict[str, object] = {
            "type": task_type,
            "websiteURL": url,
            "websiteKey": site_key,
        }
        if action is not None and kind == "recaptcha-v3":
            task_payload["pageAction"] = action
        if extra:
            task_payload.update(extra)

        async with httpx.AsyncClient() as client:
            create = await self._post_json(
                client,
                f"{self._api_base}/createTask",
                {"clientKey": self._api_key, "task": task_payload},
            )
            err = create.get("errorId")
            if isinstance(err, int) and err != 0:
                msg = f"CapSolver createTask error: {create.get('errorDescription')}"
                raise CaptchaSolveError(msg)
            task_id = create.get("taskId")
            if not task_id:
                raise CaptchaSolveError("CapSolver createTask returned no taskId")

            return await self._poll_capsolver(client, str(task_id))

    async def _poll_capsolver(self, client: httpx.AsyncClient, task_id: str) -> str:
        deadline = asyncio.get_event_loop().time() + self._timeout_s
        delay = 1.5
        while True:
            if asyncio.get_event_loop().time() >= deadline:
                raise CaptchaSolveError("CapSolver poll timed out")
            await asyncio.sleep(delay)
            body = await self._post_json(
                client,
                f"{self._api_base}/getTaskResult",
                {"clientKey": self._api_key, "taskId": task_id},
            )
            status = body.get("status")
            if status == "ready":
                solution = body.get("solution") or {}
                if not isinstance(solution, dict):
                    raise CaptchaSolveError("CapSolver: unexpected solution shape")
                token = (
                    solution.get("token")
                    or solution.get("gRecaptchaResponse")
                    or solution.get("captchaResponse")
                )
                if not token:
                    raise CaptchaSolveError("CapSolver: solution has no token")
                return str(token)
            if status == "processing":
                delay = 2.0  # back off slightly after first poll
                continue
            err = body.get("errorDescription") or body.get("errorCode") or "(no detail)"
            msg = f"CapSolver task failed: {err}"
            raise CaptchaSolveError(msg)


def _capsolver_task_type(kind: CaptchaKind) -> str:
    """Map our normalized kind to CapSolver's task-type strings."""
    return {
        "turnstile": "AntiTurnstileTaskProxyLess",
        "hcaptcha": "HCaptchaTaskProxyless",
        "recaptcha-v2": "ReCaptchaV2TaskProxyless",
        "recaptcha-v3": "ReCaptchaV3TaskProxyless",
        "image": "ImageToTextTask",
        "funcaptcha": "FunCaptchaTaskProxyless",
        "arkose": "FunCaptchaTaskProxyless",
        "geetest": "GeeTestTaskProxyless",
        "aws-waf": "AntiAwsWafTaskProxyless",
        "datadome": "DatadomeSliderTask",
    }[kind]


class NopechaSolver(_PaidSolverBase):
    """Tier 2 — NopeCHA. Has a free dev tier; smaller coverage."""

    name = "nopecha"
    _api_base = "https://api.nopecha.com"

    @property
    def supported(self) -> frozenset[CaptchaKind]:
        return frozenset({"turnstile", "hcaptcha", "recaptcha-v2", "recaptcha-v3"})

    async def solve(
        self,
        kind: CaptchaKind,
        site_key: str,
        url: str,
        *,
        action: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        if kind not in self.supported:
            msg = f"NopechaSolver doesn't support {kind!r}"
            raise CaptchaSolveError(msg)
        nopetype = {
            "turnstile": "turnstile",
            "hcaptcha": "hcaptcha",
            "recaptcha-v2": "recaptcha2",
            "recaptcha-v3": "recaptcha3",
        }[kind]
        payload: dict[str, object] = {
            "key": self._api_key,
            "type": nopetype,
            "url": url,
            "sitekey": site_key,
        }
        if action and kind == "recaptcha-v3":
            payload["data"] = {"action": action}
        if extra:
            payload.update(extra)

        async with httpx.AsyncClient() as client:
            create = await self._post_json(client, f"{self._api_base}/token", payload)
            ticket = create.get("data")
            if not ticket:
                err = create.get("error") or "(no detail)"
                raise CaptchaSolveError(f"NopeCHA token request failed: {err}")
            # NopeCHA returns the token directly when ready, or a ticket
            # we then poll. Both shapes accepted.
            if isinstance(ticket, str):
                return ticket
            ticket_id = ticket if isinstance(ticket, str) else create.get("id")
            if not ticket_id:
                raise CaptchaSolveError("NopeCHA returned no ticket id")
            return await self._poll_nopecha(client, str(ticket_id))

    async def _poll_nopecha(self, client: httpx.AsyncClient, ticket_id: str) -> str:
        deadline = asyncio.get_event_loop().time() + self._timeout_s
        while True:
            if asyncio.get_event_loop().time() >= deadline:
                raise CaptchaSolveError("NopeCHA poll timed out")
            await asyncio.sleep(2.0)
            body = await self._post_json(
                client, f"{self._api_base}/token", {"key": self._api_key, "id": ticket_id}
            )
            data = body.get("data")
            if isinstance(data, str) and data:
                return data
            err = body.get("error")
            if err:
                raise CaptchaSolveError(f"NopeCHA poll error: {err}")


class TwoCaptchaSolver(_PaidSolverBase):
    """Tier 2 — 2Captcha. Human-powered fallback, broadest coverage."""

    name = "twocaptcha"
    _api_base = "https://2captcha.com"

    @property
    def supported(self) -> frozenset[CaptchaKind]:
        return frozenset(
            {
                "turnstile",
                "hcaptcha",
                "recaptcha-v2",
                "recaptcha-v3",
                "image",
                "funcaptcha",
                "arkose",
                "geetest",
            }
        )

    async def solve(
        self,
        kind: CaptchaKind,
        site_key: str,
        url: str,
        *,
        action: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        method = {
            "turnstile": "turnstile",
            "hcaptcha": "hcaptcha",
            "recaptcha-v2": "userrecaptcha",
            "recaptcha-v3": "userrecaptcha",
            "image": "base64",
            "funcaptcha": "funcaptcha",
            "arkose": "funcaptcha",
            "geetest": "geetest",
        }[kind]
        params: dict[str, str] = {
            "key": self._api_key,
            "method": method,
            "json": "1",
            "pageurl": url,
            "sitekey": site_key,
        }
        if kind == "recaptcha-v3":
            params["version"] = "v3"
            if action:
                params["action"] = action
        if extra:
            for k, v in extra.items():
                params[k] = str(v)

        async with httpx.AsyncClient() as client:
            in_resp = await client.get(f"{self._api_base}/in.php", params=params, timeout=15.0)
            in_resp.raise_for_status()
            payload = in_resp.json()
            if payload.get("status") != 1:
                raise CaptchaSolveError(f"2Captcha submit failed: {payload.get('request')}")
            captcha_id = str(payload.get("request"))

            return await self._poll_twocaptcha(client, captcha_id)

    async def _poll_twocaptcha(self, client: httpx.AsyncClient, captcha_id: str) -> str:
        deadline = asyncio.get_event_loop().time() + self._timeout_s
        # 2Captcha asks for a 5-second initial delay before polling.
        await asyncio.sleep(5.0)
        while True:
            if asyncio.get_event_loop().time() >= deadline:
                raise CaptchaSolveError("2Captcha poll timed out")
            resp = await client.get(
                f"{self._api_base}/res.php",
                params={"key": self._api_key, "action": "get", "id": captcha_id, "json": "1"},
                timeout=15.0,
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("status") == 1:
                return str(body.get("request"))
            req = body.get("request")
            if req == "CAPCHA_NOT_READY":
                await asyncio.sleep(5.0)
                continue
            raise CaptchaSolveError(f"2Captcha poll failed: {req}")


# --- Cascade orchestrator -------------------------------------------------


class NoSolver:
    """Explicit opt-out — captcha encounter raises ``CaptchaSolveError``."""

    name = "none"
    requires_api_key = False

    @property
    def supported(self) -> frozenset[CaptchaKind]:
        return frozenset()

    async def solve(
        self,
        kind: CaptchaKind,
        site_key: str,
        url: str,
        *,
        action: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        msg = (
            f"Captcha encountered ({kind} on {url}) but solver is disabled. "
            "Configure SCRAPPER_TOOL_CAPTCHA_KEY or set captcha_solver to a real solver."
        )
        raise CaptchaSolveError(msg)


class AutoCascadeSolver:
    """Tries each tier in order, returning the first non-empty token.

    Tier 0 (Camoufox auto-pass) returns "" — that's the signal the caller
    should reload the page and check whether the challenge is gone. If it
    *is* gone the caller treats "" as success. If not, the cascade moves
    to Tier 1 / Tier 2.
    """

    name = "auto"
    requires_api_key = False

    @property
    def supported(self) -> frozenset[CaptchaKind]:
        # Conservative: claim the union of free tiers' kinds; paid tiers
        # add coverage when an api key is present.
        return frozenset({"turnstile", "hcaptcha", "recaptcha-v2", "recaptcha-v3"})

    def __init__(self, *, tiers: list[CaptchaSolver]) -> None:
        if not tiers:
            msg = "AutoCascadeSolver requires at least one tier"
            raise ValueError(msg)
        self._tiers = tiers

    async def solve(
        self,
        kind: CaptchaKind,
        site_key: str,
        url: str,
        *,
        action: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        last_error: CaptchaSolveError | None = None
        for solver in self._tiers:
            if kind not in solver.supported:
                continue
            try:
                _logger.info("agent.captcha.try_tier", solver=solver.name, kind=kind, url=url)
                token = await solver.solve(kind, site_key, url, action=action, extra=extra)
                _logger.info(
                    "agent.captcha.tier_succeeded",
                    solver=solver.name,
                    token_kind="empty" if not token else "non-empty",
                )
                return token
            except CaptchaSolveError as exc:
                _logger.warning("agent.captcha.tier_failed", solver=solver.name, error=str(exc))
                last_error = exc
                continue
        if last_error is not None:
            raise CaptchaSolveError(
                f"All captcha tiers failed for {kind} on {url}. Last error: {last_error}"
            ) from last_error
        msg = (
            f"No captcha tier handles {kind!r}. Configure a paid solver "
            "(SCRAPPER_TOOL_CAPTCHA_KEY) or use captcha_solver='none' to fail fast."
        )
        raise CaptchaSolveError(msg)


# --- Resolver -------------------------------------------------------------


def get_captcha_solver(config: AgentConfig) -> CaptchaSolver:  # noqa: PLR0911, PLR0912
    """Build a captcha solver from config.

    ``"auto"`` builds the cascade. The other values map to single-tier
    solvers, useful for tests or when the user wants a specific tier.
    """
    name = config.captcha_solver
    api_key = config.captcha_api_key.get_secret_value() if config.captcha_api_key else None
    timeout_s = config.captcha_timeout_s

    if name == "none":
        return NoSolver()
    if name == "camoufox-auto":
        return CamoufoxAutoSolver()
    if name == "theyka":
        return TheykaSolver()
    if name == "capsolver":
        if not api_key:
            return NoSolver()
        return CapSolverSolver(api_key=api_key, timeout_s=timeout_s)
    if name == "nopecha":
        if not api_key:
            return NoSolver()
        return NopechaSolver(api_key=api_key, timeout_s=timeout_s)
    if name == "twocaptcha":
        if not api_key:
            return NoSolver()
        return TwoCaptchaSolver(api_key=api_key, timeout_s=timeout_s)
    if name == "auto":
        tiers: list[CaptchaSolver] = [CamoufoxAutoSolver(), TheykaSolver()]
        if api_key:
            chosen = config.captcha_paid_fallback
            paid_solver: CaptchaSolver | None = None
            if chosen == "capsolver":
                paid_solver = CapSolverSolver(api_key=api_key, timeout_s=timeout_s)
            elif chosen == "nopecha":
                paid_solver = NopechaSolver(api_key=api_key, timeout_s=timeout_s)
            elif chosen == "twocaptcha":
                paid_solver = TwoCaptchaSolver(api_key=api_key, timeout_s=timeout_s)
            if paid_solver is not None:
                tiers.append(paid_solver)
        return AutoCascadeSolver(tiers=tiers)

    raise ValueError(f"Unknown captcha solver: {name!r}")  # type: ignore[unreachable, unused-ignore]  # pragma: no cover


__all__ = [
    "AutoCascadeSolver",
    "CamoufoxAutoSolver",
    "CapSolverSolver",
    "CaptchaKind",
    "CaptchaSolver",
    "NoSolver",
    "NopechaSolver",
    "TheykaSolver",
    "TwoCaptchaSolver",
    "get_captcha_solver",
]
