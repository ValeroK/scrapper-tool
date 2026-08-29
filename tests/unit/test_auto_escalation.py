"""The automatic-escalation surface: E2's learned gate and backend capabilities.

Three changes share this file because they share one goal — the cascade should
exhaust what it has before reporting that a site cannot be scraped, and it should
say honestly which of those two things happened.

1. **E2 is reached automatically** (``interactive`` defaults to auto) and the
   decision is *learned per domain* rather than delegated to the caller.
2. **Backends carry capabilities**, so a tier that needs CDP simply never sees a
   backend that has none — the impossible combination cannot be constructed.
3. **A declined tier is distinguishable from a defeated one** in the log.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from scrapper_tool import http_server
from scrapper_tool.agent.backends.browser import (
    BACKEND_CAPABILITIES,
    BACKEND_FALLBACK_ORDER,
    backends_supporting,
)
from scrapper_tool.recipe.policy import DomainPolicy, get_policy_store


class TestBackendCapabilities:
    """Structural facts about a backend, kept separate from how well it evades."""

    def test_every_backend_in_the_fallback_order_has_capabilities(self) -> None:
        """The two tables must not drift; an unknown name would silently vanish."""
        assert set(BACKEND_FALLBACK_ORDER) == set(BACKEND_CAPABILITIES)

    def test_camoufox_is_not_a_cdp_candidate(self) -> None:
        """The whole point: Camoufox is Firefox and Firefox dropped CDP.

        browser-use attaches over CDP only, so E2 filtering on ``cdp=True`` is
        what makes ``interactive`` + ``camoufox`` — two individually correct
        settings — impossible to combine by accident. There is no 503 to explain
        because there is no way to ask for it.
        """
        assert BACKEND_CAPABILITIES["camoufox"].cdp is False
        assert "camoufox" not in backends_supporting(cdp=True)

    def test_cdp_candidates_are_the_chromium_family(self) -> None:
        candidates = backends_supporting(cdp=True)
        assert candidates == ("patchright", "obscura")
        assert all(BACKEND_CAPABILITIES[n].engine == "chromium" for n in candidates)

    def test_unfiltered_order_is_the_full_fallback_order(self) -> None:
        assert backends_supporting() == BACKEND_FALLBACK_ORDER

    def test_camoufox_leads_the_fallback_order(self) -> None:
        """Order is "try next", not "strength".

        Measured on tascaparts.com, Patchright earned a hard WAF block where
        Camoufox got a clean 200; on other targets the reverse holds. Camoufox
        leads because it is the best *single* choice, and Patchright follows
        because it changes engine — the variable most likely to change the
        outcome — not because it is second-strongest.
        """
        assert BACKEND_FALLBACK_ORDER[0] == "camoufox"
        assert BACKEND_CAPABILITIES[BACKEND_FALLBACK_ORDER[1]].engine == "chromium"


class TestE2LearnedGate:
    """``interactive`` defaults to auto; the domain policy decides.

    The old default (``False``) made the *caller* classify the page, which is the
    job the cascade exists to do — and because a gated E2 logged as an ordinary
    ``skipped`` step, a client that never forwarded the flag looked exactly like
    one whose pages did not need E2. That went unnoticed for months.
    """

    @staticmethod
    def _seed(domain: str, **kwargs: Any) -> None:
        get_policy_store()._write(  # type: ignore[attr-defined]
            domain,
            DomainPolicy(
                domain=domain,
                best_tier=kwargs.pop("best_tier", "a_b_c"),
                updated_at=datetime.now(UTC).isoformat(),
                **kwargs,
            ),
        )

    @staticmethod
    def _req(url: str, interactive: Any = None) -> Any:
        class _Req:
            def __init__(self) -> None:
                self.url = url
                self.interactive = interactive

        return _Req()

    def test_a_domain_with_no_history_gets_its_chance(self) -> None:
        """Automatic means automatic: never tried here, so try."""
        allowed, reason = http_server._e2_gate(self._req("https://fresh.test/p"))
        assert allowed is True
        assert "no history" in reason

    def test_one_failure_is_not_a_verdict(self) -> None:
        """A single loss is as likely a cold LLM or a transient block."""
        self._seed("once.test", e2_attempts=1)
        allowed, _ = http_server._e2_gate(self._req("https://once.test/p"))
        assert allowed is True

    def test_repeated_failure_without_a_win_stops_the_spending(self) -> None:
        self._seed("futile.test", e2_attempts=2)
        allowed, reason = http_server._e2_gate(self._req("https://futile.test/p"))
        assert allowed is False
        assert "learned" in reason
        assert "futile.test" in reason

    def test_a_single_win_re_enables_the_domain_permanently(self) -> None:
        """One win outranks any number of losses — E2 demonstrably works here."""
        self._seed("winner.test", e2_attempts=9, e2_wins=1)
        allowed, _ = http_server._e2_gate(self._req("https://winner.test/p"))
        assert allowed is True

    def test_explicit_true_overrides_a_learned_refusal(self) -> None:
        """A caller who knows the page needs interaction outranks our history."""
        self._seed("futile2.test", e2_attempts=5)
        allowed, reason = http_server._e2_gate(self._req("https://futile2.test/p", True))
        assert allowed is True
        assert "explicit opt-in" in reason

    def test_explicit_false_is_final(self) -> None:
        """Cost capping is the caller's decision and nothing overrides it."""
        allowed, reason = http_server._e2_gate(self._req("https://fresh2.test/p", False))
        assert allowed is False
        assert "opted out" in reason

    def test_a_broken_policy_store_still_allows_e2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail open. The bug being fixed is a tier silently NOT running.

        Resolving a lookup failure to "skip" would reintroduce exactly that, and
        the cost of failing open is one expensive tier we did not have to run.
        """

        def boom() -> Any:
            raise RuntimeError("policy store on fire")

        monkeypatch.setattr("scrapper_tool.recipe.policy.get_policy_store", boom)
        allowed, reason = http_server._e2_gate(self._req("https://any.test/p"))
        assert allowed is True
        assert "policy unavailable" in reason


class TestE2AttemptAccounting:
    """Losses are the observation that matters, and only ``record_e2_attempt``
    hears about them — ``record`` is only ever told about wins."""

    def test_a_loss_is_recorded_without_claiming_a_best_tier(self) -> None:
        """A losing tier must never be written down as the best one.

        If it were, ``start_tier_rank`` would skip every cheaper tier on the next
        request for this domain — on the strength of a tier that failed.
        """
        store = get_policy_store()
        policy = store.record_e2_attempt("https://lost.test/p", won=False)
        assert policy is not None
        assert policy.e2_attempts == 1
        assert policy.e2_wins == 0
        assert policy.best_tier == "a_b_c"
        assert policy.start_tier_rank() == 0

    def test_attempts_accumulate_until_the_domain_is_written_off(self) -> None:
        store = get_policy_store()
        store.record_e2_attempt("https://twice.test/p", won=False)
        policy = store.record_e2_attempt("https://twice.test/p", won=False)
        assert policy is not None
        assert policy.e2_attempts == 2
        assert policy.should_try_e2() is False

    def test_a_win_after_losses_re_enables_the_domain(self) -> None:
        store = get_policy_store()
        store.record_e2_attempt("https://late.test/p", won=False)
        store.record_e2_attempt("https://late.test/p", won=False)
        policy = store.record_e2_attempt("https://late.test/p", won=True)
        assert policy is not None
        assert policy.should_try_e2() is True

    def test_recording_a_win_preserves_the_learned_tier(self) -> None:
        """E2 accounting must not disturb what the cascade learned about tiers."""
        store = get_policy_store()
        store.record("https://mixed.test/p", "render")
        store.record("https://mixed.test/p", "render")
        policy = store.record_e2_attempt("https://mixed.test/p", won=False)
        assert policy is not None
        assert policy.best_tier == "render"
        assert policy.observations == 2

    def test_old_policy_files_load_without_the_new_fields(self) -> None:
        """Forward compatibility: files written before E2 accounting existed."""
        policy = DomainPolicy.from_dict({"domain": "old.test", "best_tier": "d"})
        assert policy.e2_attempts == 0
        assert policy.best_backend is None
        assert policy.should_try_e2() is True


class TestE2BackendSubstitution:
    """E2 gets a backend that can host it, without the caller knowing why."""

    @staticmethod
    def _cfg(browser: str) -> Any:
        from scrapper_tool.agent.types import AgentConfig

        return AgentConfig(browser=browser)

    @staticmethod
    def _req(browser: str | None = None) -> Any:
        class _Req:
            def __init__(self) -> None:
                self.url = "https://x.test/p"
                self.browser = browser

        return _Req()

    def test_camoufox_is_swapped_for_a_cdp_backend(self) -> None:
        req = self._req()
        out = http_server._cfg_for_e2_backend(req, self._cfg("camoufox"))
        assert out.browser == "patchright"
        assert req.__dict__["_e2_backend_substituted"] == ("camoufox", "patchright")

    def test_a_cdp_capable_backend_is_left_alone(self) -> None:
        out = http_server._cfg_for_e2_backend(self._req(), self._cfg("patchright"))
        assert out.browser == "patchright"

    def test_an_explicit_caller_choice_always_wins(self) -> None:
        """Overriding a named backend would be the silent downgrade, inverted.

        The caller asked for camoufox by name; honouring it means E2 raises a
        message that explains itself, which is far better than quietly running
        somewhere else.
        """
        req = self._req(browser="camoufox")
        out = http_server._cfg_for_e2_backend(req, self._cfg("camoufox"))
        assert out.browser == "camoufox"
        assert "_e2_backend_substituted" not in req.__dict__

    def test_the_substitution_is_reported_in_the_escalation_log(self) -> None:
        """A caller comparing E2 against D/E1 must know they differed."""
        req = self._req()
        http_server._cfg_for_e2_backend(req, self._cfg("camoufox"))
        detail = http_server._e2_backend_detail(req)
        assert detail is not None
        assert "camoufox" in detail
        assert "patchright" in detail
        assert "no CDP" in detail

    def test_no_note_when_nothing_was_substituted(self) -> None:
        assert http_server._e2_backend_detail(self._req()) is None
