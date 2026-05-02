"""Humanlike behavior policy for browser interactions.

DataDome and similar 2026 anti-bot systems detect *behavior* (timing,
mouse paths, scroll cadence) rather than just fingerprint. A perfectly
spoofed Chromium that clicks at exact integer coordinates within 5 ms of
page load is still detected.

The :class:`HumanlikePolicy` injects:

- Jittered keystroke delays drawn from a log-normal distribution
  (60-180 ms median).
- Bezier-curve mouse trajectories with overshoot + correction.
- Variable scroll cadence (50-300 ms between wheel events).
- Random read-time pauses on page load (300-1500 ms).

Backends call ``policy.apply_to(page)`` after each navigation; the
policy registers the per-action shaping. ``FastPolicy`` and
``OffPolicy`` are no-ops, useful for tests / low-protection sites.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any, Protocol

from scrapper_tool._logging import get_logger

_logger = get_logger(__name__)


class BehaviorPolicy(Protocol):
    """Behavior shaping applied to a Playwright/CDP page."""

    name: str

    async def pre_navigate(self) -> None:
        """Called before each ``page.goto`` — opportunity to delay."""

    async def post_navigate(self) -> None:
        """Called after the page loads — simulates "reading" pause."""

    async def shape_keystrokes(self) -> float:
        """Return per-keystroke delay in seconds (drawn from a distribution)."""

    async def shape_scroll(self) -> float:
        """Return per-scroll-tick delay in seconds."""

    def mouse_path(self, start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
        """Return a list of intermediate (x, y) waypoints between two points.

        Empty list = straight-line (default Playwright behavior).
        Bezier curves with mid-point overshoot make the path realistic.
        """


class HumanlikePolicy:
    """Default — humanlike timing and mouse paths."""

    name = "humanlike"

    def __init__(
        self,
        *,
        keystroke_median_ms: float = 110.0,
        keystroke_sigma: float = 0.45,
        scroll_min_ms: float = 50.0,
        scroll_max_ms: float = 300.0,
        post_navigate_min_ms: float = 300.0,
        post_navigate_max_ms: float = 1500.0,
        mouse_steps: int = 14,
        rng: random.Random | None = None,
    ) -> None:
        self._k_median = keystroke_median_ms
        self._k_sigma = keystroke_sigma
        self._s_min = scroll_min_ms
        self._s_max = scroll_max_ms
        self._n_min = post_navigate_min_ms
        self._n_max = post_navigate_max_ms
        self._mouse_steps = mouse_steps
        # Use injected RNG for determinism in tests. Not security-sensitive
        # — humanlike timing is for behavioral mimicry, not entropy.
        self._rng = rng or random.Random()  # noqa: S311

    async def pre_navigate(self) -> None:
        # A small jitter before navigation prevents perfectly-aligned
        # request bursts that "screams scraper".
        delay = self._rng.uniform(0.05, 0.20)
        await asyncio.sleep(delay)

    async def post_navigate(self) -> None:
        ms = self._rng.uniform(self._n_min, self._n_max)
        await asyncio.sleep(ms / 1000.0)

    async def shape_keystrokes(self) -> float:
        # Log-normal distribution centered on median_ms.
        mu = math.log(self._k_median / 1000.0)
        sample = self._rng.lognormvariate(mu, self._k_sigma)
        # Clamp to sane bounds — outliers from log-normal can be huge.
        return max(0.025, min(sample, 0.6))

    async def shape_scroll(self) -> float:
        ms = self._rng.uniform(self._s_min, self._s_max)
        return ms / 1000.0

    def mouse_path(self, start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
        x0, y0 = start
        x1, y1 = end
        # Quadratic Bezier with a control point offset perpendicular to
        # the straight line — gives a humanlike arc.
        cx = (x0 + x1) / 2 + self._rng.uniform(-30, 30)
        cy = (y0 + y1) / 2 + self._rng.uniform(-30, 30)
        path: list[tuple[int, int]] = []
        for i in range(1, self._mouse_steps):
            t = i / self._mouse_steps
            # Quadratic Bezier: B(t) = (1-t)²·P0 + 2(1-t)t·C + t²·P1
            mt = 1 - t
            x = mt * mt * x0 + 2 * mt * t * cx + t * t * x1
            y = mt * mt * y0 + 2 * mt * t * cy + t * t * y1
            # Add small per-point jitter — a real hand isn't smooth.
            jitter_x = self._rng.uniform(-1.5, 1.5)
            jitter_y = self._rng.uniform(-1.5, 1.5)
            path.append((int(x + jitter_x), int(y + jitter_y)))
        return path


class FastPolicy:
    """Skip humanlike delays — for unprotected sites or speed-critical batch."""

    name = "fast"

    async def pre_navigate(self) -> None:
        return None

    async def post_navigate(self) -> None:
        return None

    async def shape_keystrokes(self) -> float:
        return 0.0

    async def shape_scroll(self) -> float:
        return 0.0

    def mouse_path(self, _start: tuple[int, int], _end: tuple[int, int]) -> list[tuple[int, int]]:
        return []


class OffPolicy(FastPolicy):
    """Alias for FastPolicy — semantically "no behavior shaping at all"."""

    name = "off"


def get_behavior_policy(name: str, *, rng: random.Random | None = None) -> BehaviorPolicy:
    if name == "humanlike":
        return HumanlikePolicy(rng=rng)
    if name == "fast":
        return FastPolicy()
    if name == "off":
        return OffPolicy()
    msg = f"Unknown behavior policy: {name!r}. Choices: 'humanlike', 'fast', 'off'."
    raise ValueError(msg)


# --- Helpers used by browser backends -------------------------------------


async def humanlike_type(page: Any, selector: str, text: str, policy: BehaviorPolicy) -> None:
    """Type ``text`` into ``selector`` with humanlike per-key delays.

    Generic enough to work across Playwright-shaped APIs (Camoufox,
    Patchright). Backends that drive raw CDP wrap this with their own
    keystroke primitive.
    """
    locator = page.locator(selector) if hasattr(page, "locator") else page
    for char in text:
        await locator.type(char) if hasattr(locator, "type") else None
        await asyncio.sleep(await policy.shape_keystrokes())


__all__ = [
    "BehaviorPolicy",
    "FastPolicy",
    "HumanlikePolicy",
    "OffPolicy",
    "get_behavior_policy",
    "humanlike_type",
]
