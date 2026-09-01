"""`blocked` must carry its evidence, or it is not a block.

`/capabilities` states that `blocked` is "true ONLY on evidence of blocking" and
that `challenge_detected` names the evidence. Those two sentences are a contract.
A consumer reported a payload that broke it: a 27 KB body carrying exactly the
content requested, `blocked=True`, `challenge_detected` empty, no redirect. They
had to treat the flag as advisory and re-derive the answer themselves, which is
the flag failing at its only job.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool import http_server
from scrapper_tool._challenge import block_evidence, looks_like_block_message


class TestBlockEvidence:
    """The verdict and the reason are produced together, so they cannot disagree."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("navigation failed: geo.captcha-delivery.com", "datadome"),
            ("blocked by validate.perfdrive.com", "radware"),
            ("HTTP 403 forbidden", "403"),
            ("Failed to solve the challenge", "challenge"),
            ("net::ERR_NAME_NOT_RESOLVED", None),
            ("", None),
        ],
    )
    def test_evidence_is_named(self, message: str, expected: str | None) -> None:
        assert block_evidence(message) == expected

    def test_a_vendor_name_beats_a_generic_term(self) -> None:
        """ "datadome" is strictly more useful to a caller than "captcha"."""
        assert block_evidence("captcha at geo.captcha-delivery.com") == "datadome"

    def test_the_boolean_and_the_evidence_can_never_disagree(self) -> None:
        """The bool is derived from the evidence, not computed separately."""
        for message in [
            "geo.captcha-delivery.com",
            "403",
            "nothing suspicious here",
            "",
            "net::ERR_CONNECTION_REFUSED",
        ]:
            assert looks_like_block_message(message) is (block_evidence(message) is not None)


class TestE1BlockedRequiresAnEmptyResult:
    """The reported bug: a verdict drawn from prose while the payload held the data."""

    @staticmethod
    def _result(**kwargs: Any) -> Any:
        class _CrawlResult:
            def __init__(self) -> None:
                self.success = kwargs.get("success", False)
                self.extracted_content = kwargs.get("extracted")
                self.markdown = kwargs.get("markdown", "stub markdown")
                self.url = "https://vendor.test/p"
                self.error_message = kwargs.get("error_message", "")

        return _CrawlResult()

    @staticmethod
    def _convert(result: Any) -> Any:
        from scrapper_tool.agent.extract import _crawl4ai_result_to_agent

        return _crawl4ai_result_to_agent(
            result, url="https://vendor.test/p", duration_s=0.1, fallback_schema=False
        )

    def test_extracted_data_means_we_were_not_blocked(self) -> None:
        """Crawl4AI reports success=False for a 403 that JavaScript then rendered.

        Its message names the vendor, and judging on that message alone returned
        blocked=True on a page carrying exactly the requested content. The
        requested schema cannot be extracted from a challenge page, so data in
        hand settles it.
        """
        agent = self._convert(
            self._result(
                success=False,
                extracted={"frame_code": "ABC-123"},
                error_message="Page returned a Cloudflare challenge",
            )
        )
        assert agent.blocked is False
        assert agent.challenge_vendor is None
        assert agent.data == {"frame_code": "ABC-123"}

    def test_no_data_plus_block_evidence_is_still_a_block(self) -> None:
        """The fix must not cost us real blocks."""
        agent = self._convert(
            self._result(
                success=False,
                extracted=None,
                error_message="Page returned a Cloudflare challenge",
            )
        )
        assert agent.blocked is True
        assert agent.challenge_vendor == "challenge"

    def test_a_body_alone_is_not_evidence_of_success(self) -> None:
        """A challenge page has a body too.

        Treating any non-empty body as success would let a wall through as
        content -- the opposite failure, and the worse one.
        """
        agent = self._convert(
            self._result(
                success=False,
                extracted=None,
                markdown="Please verify you are human",
                error_message="datadome captcha",
            )
        )
        assert agent.blocked is True
        assert agent.challenge_vendor == "datadome"


class TestBlockedInvariant:
    """A contradiction the library refuses to emit."""

    @staticmethod
    def _req() -> Any:
        return http_server.ScrapeRequest(url="https://vendor.test/p")

    def test_evidence_is_recovered_from_the_error_when_a_tier_forgot(self) -> None:
        payload: dict[str, Any] = {
            "blocked": True,
            "challenge_detected": None,
            "error": "blocked by geo.captcha-delivery.com",
        }
        http_server._enforce_blocked_invariant(payload, self._req())
        assert payload["blocked"] is True
        assert payload["challenge_detected"] == "datadome"

    def test_an_unfounded_block_claim_is_withdrawn(self) -> None:
        """The reported payload: a wall asserted with nothing behind it.

        Withdrawing is safe *because* the tiers now judge on outcome rather than
        prose -- reaching here with no evidence means no tier saw a wall, and
        publishing an unfounded block is the failure being fixed.
        """
        payload: dict[str, Any] = {
            "blocked": True,
            "challenge_detected": None,
            "error": None,
            "pattern_used": "e1",
        }
        http_server._enforce_blocked_invariant(payload, self._req())
        assert payload["blocked"] is False
        assert payload["challenge_detected"] is None

    def test_a_block_that_named_its_evidence_is_left_alone(self) -> None:
        payload: dict[str, Any] = {
            "blocked": True,
            "challenge_detected": "cloudflare",
            "error": "whatever",
        }
        http_server._enforce_blocked_invariant(payload, self._req())
        assert payload["blocked"] is True
        assert payload["challenge_detected"] == "cloudflare"

    def test_an_unblocked_payload_is_never_touched(self) -> None:
        payload: dict[str, Any] = {"blocked": False, "challenge_detected": None, "error": None}
        http_server._enforce_blocked_invariant(payload, self._req())
        assert payload["blocked"] is False

    def test_the_invariant_holds_for_every_shape_reaching_a_caller(self) -> None:
        """The property, stated once: blocked implies named evidence."""
        shapes: list[dict[str, Any]] = [
            {"blocked": True, "challenge_detected": None, "error": "403 forbidden"},
            {"blocked": True, "challenge_detected": None, "error": None},
            {"blocked": True, "challenge_detected": "akamai", "error": None},
            {"blocked": False, "challenge_detected": None, "error": "harmless"},
        ]
        for payload in shapes:
            http_server._enforce_blocked_invariant(payload, self._req())
            assert not payload["blocked"] or payload["challenge_detected"], payload
