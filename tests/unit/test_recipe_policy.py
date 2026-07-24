"""Unit tests for per-domain tier memory (F2).

The store is deliberately conservative — it can only ever *save* work by skipping
cheaper tiers, never cause a wrong answer, because the cascade still falls through
from wherever it starts. So these tests focus on the two ways the conservatism
could be wrong: trusting a fluke too early, and pinning a domain to a stale tier
after the site changed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from scrapper_tool.recipe.policy import (
    DomainPolicy,
    DomainPolicyStore,
    domain_policy_enabled,
    tier_rank,
)


@pytest.fixture
def store(tmp_path: Path) -> DomainPolicyStore:
    return DomainPolicyStore(tmp_path)


# --- tier ordering ----------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "expected"),
    [("a_b_c", 0), ("d", 1), ("render", 2), ("e1", 3), ("e2", 4), ("replay", -1), ("x", -1)],
)
def test_tier_rank(tier: str, expected: int) -> None:
    assert tier_rank(tier) == expected


# --- confidence: don't trust a fluke ----------------------------------------


def test_one_win_is_not_confident(store: DomainPolicyStore) -> None:
    """A single success could be a proxy rotation or a block briefly lifting."""
    policy = store.record("https://mopar.com/p", "render")
    assert policy is not None
    assert policy.observations == 1
    assert policy.is_confident is False
    assert policy.start_tier_rank() == 0, "an unconfident policy skips nothing"


def test_two_wins_at_the_same_tier_earn_confidence(store: DomainPolicyStore) -> None:
    store.record("https://mopar.com/a", "render")
    policy = store.record("https://mopar.com/b", "render")
    assert policy is not None
    assert policy.observations == 2
    assert policy.is_confident is True
    assert policy.start_tier_rank() == tier_rank("render")


def test_confidence_accumulates_across_paths_on_one_domain(store: DomainPolicyStore) -> None:
    """Different paths on the same host share the memory, and www is folded in.

    Subdomains are NOT collapsed — registrable_domain only strips ``www.`` and
    deliberately avoids public-suffix logic (``store.x.com`` stays distinct from
    ``x.com``), so this uses one host plus its www alias, which is the real shape
    of a vendor's product URLs.
    """
    store.record("https://www.mopar.com/x", "render")
    store.record("https://mopar.com/y", "render")
    policy = store.get("https://mopar.com/z")
    assert policy is not None
    assert policy.observations == 2


# --- the site changed -------------------------------------------------------


def test_a_win_at_a_different_tier_resets_confidence(store: DomainPolicyStore) -> None:
    """The ladder started working again, or render stopped — old count is stale."""
    store.record("https://x.test/a", "render")
    store.record("https://x.test/b", "render")  # confident at render
    changed = store.record("https://x.test/c", "a_b_c")
    assert changed is not None
    assert changed.best_tier == "a_b_c"
    assert changed.observations == 1, "adopting a new tier resets the count"
    assert changed.is_confident is False


def test_expiry_forces_rediscovery(store: DomainPolicyStore) -> None:
    """The only thing that re-probes cheap tiers on a learned-skip domain."""
    old = datetime.now(UTC) - timedelta(hours=25)
    store.record("https://x.test/a", "render", now=old)
    store.record("https://x.test/b", "render", now=old)
    assert store.get("https://x.test/c") is None, "a policy past its TTL is ignored"


def test_fresh_policy_survives(store: DomainPolicyStore) -> None:
    recent = datetime.now(UTC) - timedelta(hours=1)
    store.record("https://x.test/a", "render", now=recent)
    store.record("https://x.test/b", "render", now=recent)
    assert store.get("https://x.test/c") is not None


def test_ttl_zero_disables_expiry(tmp_path: Path) -> None:
    store = DomainPolicyStore(tmp_path, ttl_s=0)
    store.record("https://x.test/a", "render", now=datetime(2020, 1, 1, tzinfo=UTC))
    store.record("https://x.test/b", "render", now=datetime(2020, 1, 1, tzinfo=UTC))
    assert store.get("https://x.test/c") is not None


# --- what is / isn't recorded -----------------------------------------------


def test_replay_wins_are_not_recorded(store: DomainPolicyStore) -> None:
    """replay is tried unconditionally before any policy, so it's not a tier
    the policy chooses between — recording it would be meaningless."""
    assert store.record("https://x.test/p", "replay") is None
    assert store.get("https://x.test/p") is None


def test_unknown_tier_is_ignored(store: DomainPolicyStore) -> None:
    assert store.record("https://x.test/p", "teleport") is None


def test_a_urlless_input_records_nothing(store: DomainPolicyStore) -> None:
    assert store.record("not a url", "render") is None


# --- fault tolerance: a policy problem must never break a scrape -------------


def test_corrupt_file_is_a_miss_and_self_heals(store: DomainPolicyStore) -> None:
    store.record("https://x.test/a", "render")
    path = next(store.directory.glob("*.json"))
    path.write_text("{ broken", encoding="utf-8")
    assert store.get("https://x.test/a") is None
    assert not path.exists()


def test_wrong_shape_json_is_a_miss(store: DomainPolicyStore) -> None:
    store.record("https://x.test/a", "render")
    next(store.directory.glob("*.json")).write_text(json.dumps({"nope": 1}), encoding="utf-8")
    assert store.get("https://x.test/a") is None


def test_unwritable_directory_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir", encoding="utf-8")
    store = DomainPolicyStore(blocker)
    assert store.record("https://x.test/p", "render") is not None  # returns policy, logs failure
    assert store.get("https://x.test/p") is None


def test_invalidate(store: DomainPolicyStore) -> None:
    store.record("https://x.test/a", "render")
    store.invalidate("https://x.test/a")
    assert store.get("https://x.test/a") is None


def test_path_traversal_stays_inside_the_dir(tmp_path: Path) -> None:
    store = DomainPolicyStore(tmp_path)
    store.record("https://x.test/a", "render")  # domain x.test is the key
    for f in tmp_path.glob("*.json"):
        assert f.parent == tmp_path


# --- round trip + config ----------------------------------------------------


def test_round_trip(store: DomainPolicyStore) -> None:
    store.record("https://x.test/a", "e1", challenge_vendor="cloudflare")
    got = store.get("https://x.test/a")
    assert got is not None
    assert got.best_tier == "e1"
    assert got.challenge_vendor == "cloudflare"
    assert DomainPolicy.from_dict(got.to_dict()) == got


def test_challenge_vendor_persists_across_updates(store: DomainPolicyStore) -> None:
    store.record("https://x.test/a", "render", challenge_vendor="radware")
    updated = store.record("https://x.test/b", "render")  # no vendor passed
    assert updated is not None
    assert updated.challenge_vendor == "radware", "the known vendor shouldn't be lost"


def test_policy_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRAPPER_TOOL_DOMAIN_POLICY", raising=False)
    assert domain_policy_enabled() is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("0", False), ("no", False), ("", True)],
)
def test_policy_toggle(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_DOMAIN_POLICY", value)
    assert domain_policy_enabled() is expected
