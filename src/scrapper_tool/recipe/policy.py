"""Per-domain tier memory — the self-tuning half of the cascade (F2).

The recipe store remembers *how* to extract a domain once we've reached content.
This remembers *which tier reaches content at all*, which is the other recurring
waste: on a domain where the HTTP ladder 403s every time and only a render gets
through (store.mopar.com, g2.com — see the 2026-07 live validation), paying for
the full A/B/C ladder and Pattern D on every request just to watch them fail is
pure latency. Once we've seen render win there twice, we start at render.

The rules that keep this from going wrong:

- **It only ever skips *cheaper* tiers, never jumps past a working one.** The
  worst case of a wrong "start at render" is one wasted browser launch, and the
  cascade still falls through to the LLM tiers from there — the policy is a
  starting hint, not a cap.
- **Confidence before commitment.** A single success could be a fluke (a proxy
  rotation, a transient block lifting), so a domain isn't trusted until it has
  won at the same tier ``_MIN_OBSERVATIONS`` times. Below that, the full cascade
  runs and keeps learning.
- **It expires.** A site that tightened *or relaxed* its anti-bot posture must be
  re-discovered, so a policy past its TTL is ignored and the next full run
  re-records. That TTL is the only thing that re-probes the cheap tiers on a
  domain we've learned to skip them on — without it, a site that got easier would
  be stuck on the expensive tier forever.

Reuses :class:`~scrapper_tool.recipe.store.JsonFileRecipeStore`'s fault
philosophy wholesale: every read failure degrades to "no policy", because the
worst case of forgetting is one full-price cascade — exactly the status quo.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scrapper_tool._logging import get_logger
from scrapper_tool.recipe.derive import registrable_domain
from scrapper_tool.recipe.store import default_cache_dir

_logger = get_logger(__name__)

# Cheapest → most expensive. The index in this tuple IS the cost ordering the
# skip logic reasons about; "replay" is deliberately absent because it's tried
# unconditionally before any policy consultation (a cache hit is cheaper than
# every tier here, so there's nothing to decide).
TIER_ORDER: tuple[str, ...] = ("a_b_c", "d", "render", "e1", "e2")

_DEFAULT_TTL_S = 24 * 3600.0
# A domain must win at the same tier this many times before we trust the memory
# enough to skip cheaper tiers on it. One win is too easy to get by luck.
_MIN_OBSERVATIONS = 2

_UNSAFE_KEY_CHARS = re.compile(r"[^a-z0-9._-]+")


def domain_policy_enabled() -> bool:
    """On by default; ``SCRAPPER_TOOL_DOMAIN_POLICY=0`` disables."""
    raw = os.environ.get("SCRAPPER_TOOL_DOMAIN_POLICY")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def tier_rank(tier: str) -> int:
    """Cost rank of a tier; -1 for anything not in the ordering (e.g. replay)."""
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return -1


@dataclass(frozen=True)
class DomainPolicy:
    """What we've learned about reaching content on one domain."""

    domain: str
    best_tier: str
    updated_at: str
    observations: int = 1
    challenge_vendor: str | None = None

    @property
    def is_confident(self) -> bool:
        """Whether this policy is trusted enough to skip cheaper tiers."""
        return self.observations >= _MIN_OBSERVATIONS and tier_rank(self.best_tier) > 0

    def start_tier_rank(self) -> int:
        """The lowest tier rank the cascade should bother attempting.

        0 (start from the top) unless the policy is confident, in which case the
        rank of the tier that's been winning — cheaper tiers get skipped.
        """
        return tier_rank(self.best_tier) if self.is_confident else 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DomainPolicy:
        return cls(
            domain=str(raw["domain"]),
            best_tier=str(raw["best_tier"]),
            updated_at=str(raw.get("updated_at", "")),
            observations=int(raw.get("observations", 1)),
            challenge_vendor=raw.get("challenge_vendor"),
        )


class DomainPolicyStore:
    """One JSON file per domain under ``<cache>/policy/``. No locking.

    A concurrent writer race just means one observation is lost, which the
    confidence counter tolerates by design.
    """

    def __init__(self, directory: Path | str | None = None, *, ttl_s: float | None = None) -> None:
        base = Path(directory) if directory is not None else default_cache_dir() / "policy"
        self.directory = base
        self.ttl_s = _DEFAULT_TTL_S if ttl_s is None else ttl_s

    def _path(self, domain: str) -> Path:
        safe = _UNSAFE_KEY_CHARS.sub("_", domain.lower()).strip("._-") or "unknown"
        return self.directory / f"{safe}.json"

    def get(self, url: str, *, now: datetime | None = None) -> DomainPolicy | None:
        domain = registrable_domain(url)
        if not domain:
            return None
        path = self._path(domain)
        try:
            policy = DomainPolicy.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _logger.info("policy.unreadable", domain=domain, error=str(exc)[:120])
            self._invalidate(domain)
            return None
        if self._expired(policy, now=now):
            _logger.info("policy.expired", domain=domain, best_tier=policy.best_tier)
            self._invalidate(domain)
            return None
        return policy

    def _expired(self, policy: DomainPolicy, *, now: datetime | None) -> bool:
        if self.ttl_s <= 0:
            return False
        try:
            updated = datetime.fromisoformat(policy.updated_at)
        except ValueError:
            return True
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return (current - updated).total_seconds() > self.ttl_s

    def record(
        self,
        url: str,
        winning_tier: str,
        *,
        challenge_vendor: str | None = None,
        now: datetime | None = None,
    ) -> DomainPolicy | None:
        """Note that ``winning_tier`` reached content on ``url``'s domain.

        Observations at the SAME tier accumulate confidence. A win at a
        *different* tier resets the count to 1 and adopts the new tier — the site
        changed, so old confidence is stale. Not recorded for tiers outside the
        ordering (replay handles itself; unknown tiers are ignored).
        """
        domain = registrable_domain(url)
        if not domain or tier_rank(winning_tier) < 0:
            return None
        existing = self.get(url, now=now)
        if existing is not None and existing.best_tier == winning_tier:
            observations = existing.observations + 1
        else:
            observations = 1
        policy = DomainPolicy(
            domain=domain,
            best_tier=winning_tier,
            updated_at=(now or datetime.now(UTC)).isoformat(),
            observations=observations,
            challenge_vendor=challenge_vendor or (existing.challenge_vendor if existing else None),
        )
        self._write(domain, policy)
        return policy

    def _write(self, domain: str, policy: DomainPolicy) -> None:
        import tempfile  # noqa: PLC0415

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.directory, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(policy.to_dict(), handle, ensure_ascii=False, indent=2)
                temp_path = Path(handle.name)
            temp_path.replace(self._path(domain))
        except OSError as exc:
            _logger.warning("policy.write_failed", domain=domain, error=str(exc)[:120])
            return
        _logger.info(
            "policy.recorded",
            domain=domain,
            best_tier=policy.best_tier,
            observations=policy.observations,
        )

    def _invalidate(self, domain: str) -> None:
        try:
            self._path(domain).unlink()
        except (FileNotFoundError, OSError):
            return

    def invalidate(self, url: str) -> None:
        domain = registrable_domain(url)
        if domain:
            self._invalidate(domain)


_default_store: DomainPolicyStore | None = None


def get_policy_store() -> DomainPolicyStore:
    """Process-wide default policy store (lazily created)."""
    global _default_store  # noqa: PLW0603
    if _default_store is None:
        _default_store = DomainPolicyStore()
    return _default_store


def set_policy_store(store: DomainPolicyStore | None) -> None:
    """Swap the default policy store. Pass None to reset (tests)."""
    global _default_store  # noqa: PLW0603
    _default_store = store


__all__ = [
    "TIER_ORDER",
    "DomainPolicy",
    "DomainPolicyStore",
    "domain_policy_enabled",
    "get_policy_store",
    "set_policy_store",
    "tier_rank",
]
