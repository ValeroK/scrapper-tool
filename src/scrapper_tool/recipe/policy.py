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
# E2 is the only tier expensive enough that "try it once to find out" is itself a
# cost worth remembering. A domain gets this many genuine attempts before the
# cascade stops reaching for it; a single win at any point re-enables it forever
# (until the TTL expires the whole policy). Two, not one, because the first
# failure is as likely to be a cold LLM or a transient block as a real verdict.
_E2_FUTILE_ATTEMPTS = 2

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
    # Which browser backend won at ``best_tier``. Separate from the tier because
    # the two escalate independently: the cascade can be confident about the tier
    # while still searching for a backend that gets through.
    best_backend: str | None = None
    # E2 futility accounting. Attempts counts genuine runs (not gate-skips), so
    # a domain that has never been tried reads 0 and is always given its chance.
    e2_attempts: int = 0
    e2_wins: int = 0

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

    def should_try_e2(self) -> bool:
        """Whether E2 is still worth reaching for on this domain.

        The learned half of E2 gating. A domain we have never tried always gets
        its chance - that is what makes the cascade automatic rather than
        opt-in. One win keeps it enabled permanently. Only a domain that has
        genuinely failed E2 ``_E2_FUTILE_ATTEMPTS`` times without ever winning
        stops paying for it, and the policy TTL re-opens even that.
        """
        if self.e2_wins > 0:
            return True
        return self.e2_attempts < _E2_FUTILE_ATTEMPTS

    def e2_skip_reason(self) -> str:
        """Why E2 was declined, in the words the escalation log should carry."""
        return (
            f"learned: E2 failed {self.e2_attempts}x on {self.domain} "
            f"without a win; re-tried after the policy TTL expires"
        )

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
            best_backend=raw.get("best_backend"),
            e2_attempts=int(raw.get("e2_attempts", 0)),
            e2_wins=int(raw.get("e2_wins", 0)),
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
        winning_backend: str | None = None,
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
            best_backend=winning_backend or (existing.best_backend if existing else None),
            e2_attempts=existing.e2_attempts if existing else 0,
            e2_wins=existing.e2_wins if existing else 0,
        )
        self._write(domain, policy)
        return policy

    def record_e2_attempt(
        self, url: str, *, won: bool, now: datetime | None = None
    ) -> DomainPolicy | None:
        """Note that E2 actually ran on this domain, and whether it worked.

        Deliberately separate from :meth:`record`: a losing E2 changes nothing
        about which tier is best, but it is exactly the observation that decides
        whether to pay for E2 here again. Recording a *loss* is the whole point,
        and ``record`` only ever hears about wins.
        """
        domain = registrable_domain(url)
        if not domain:
            return None
        existing = self.get(url, now=now)
        policy = DomainPolicy(
            domain=domain,
            # NOT "e2": this method records losses as well as wins, and a
            # losing tier must never be written down as the best one. "a_b_c"
            # has rank 0, so a domain whose only history is a failed E2 still
            # starts the cascade from the top next time.
            best_tier=existing.best_tier if existing else "a_b_c",
            updated_at=(now or datetime.now(UTC)).isoformat(),
            observations=existing.observations if existing else 1,
            challenge_vendor=existing.challenge_vendor if existing else None,
            best_backend=existing.best_backend if existing else None,
            e2_attempts=(existing.e2_attempts if existing else 0) + 1,
            e2_wins=(existing.e2_wins if existing else 0) + (1 if won else 0),
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
