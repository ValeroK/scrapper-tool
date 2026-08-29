"""The tool's own manual, served over the wire.

Until now the skill only reached agents that had the repository checked out — it
was in the sdist but not the Docker image, and neither the HTTP sidecar nor the
MCP server offered it. The consequence was measured on a real integration: a
heavy consumer reverse-engineered the request flags from ``/openapi.json`` by
hand and learned the valid browser backends only from the text of an error
message, because nothing over the wire said what the tool could do.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from scrapper_tool import http_server
from scrapper_tool.skill import skill_markdown, skill_path

if TYPE_CHECKING:
    from pathlib import Path


def _client(app: Any) -> AsyncClient:
    """A local ASGI client.

    Deliberately duplicated from ``test_http_server`` rather than imported from
    it. ``tests`` has no ``__init__.py``, so ``from tests.unit...`` resolves only
    when the repository root happens to be on ``sys.path`` — true under
    ``python -m pytest`` (which prepends the CWD) and false under the
    ``uv run pytest`` console script that CI uses. Three lines of duplication
    beats a cross-module test import that works on one invocation and not the
    other.
    """
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestSkillResolution:
    def test_the_repository_copy_is_found(self) -> None:
        path = skill_path()
        assert path is not None
        assert path.name == "SKILL.md"

    def test_the_markdown_is_the_real_skill(self) -> None:
        markdown = skill_markdown()
        assert markdown is not None
        # The frontmatter is what makes it a skill rather than a readme.
        assert markdown.lstrip().startswith("---")
        assert "name: scrapper-tool" in markdown

    def test_an_explicit_override_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deployments vendor house rules on top of the shipped skill."""
        custom = tmp_path / "custom.md"
        custom.write_text("# house rules", encoding="utf-8")
        monkeypatch.setenv("SCRAPPER_TOOL_SKILL_PATH", str(custom))
        assert skill_markdown() == "# house rules"

    def test_a_missing_override_falls_back_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad override must not break scraping — it is only a manual."""
        monkeypatch.setenv("SCRAPPER_TOOL_SKILL_PATH", str(tmp_path / "nope.md"))
        assert skill_markdown() is not None

    def test_a_build_without_a_skill_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("scrapper_tool.skill.skill_path", lambda: None)
        assert skill_markdown() is None


class TestSkillEndpoint:
    @pytest.fixture()
    def app_no_auth(self) -> Any:
        return http_server._build_app(api_key=None, cors_origins=["*"], serve_docs=True)

    @pytest.mark.asyncio
    async def test_it_serves_markdown(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.get("/skill")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        assert "name: scrapper-tool" in resp.text

    @pytest.mark.asyncio
    async def test_a_build_without_a_skill_404s_with_a_remedy(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """404 carries what to do about it, not just what went wrong."""
        monkeypatch.setattr("scrapper_tool.skill.skill_markdown", lambda: None)

        async with _client(app_no_auth) as client:
            resp = await client.get("/skill")

        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "skill_not_bundled"
        assert "SCRAPPER_TOOL_SKILL_PATH" in body["remedy"]


class TestCapabilitiesEndpoint:
    """The startup handshake.

    A downstream integration ran a 2.1.0 client against a 3.0.0 sidecar, learned
    the request flags by reading ``/openapi.json`` by hand, and discovered the
    valid browser backends only from an enum embedded in an error message — after
    concluding E2 was structurally unavailable. All of that is contract, and it
    is now published.
    """

    @pytest.fixture()
    def app_no_auth(self) -> Any:
        return http_server._build_app(api_key=None, cors_origins=["*"])

    @pytest.mark.asyncio
    async def test_it_publishes_the_valid_backend_names(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            body = (await client.get("/capabilities")).json()

        names = [b["name"] for b in body["browsers"]]
        assert names == ["camoufox", "patchright", "obscura", "scrapling"]

    @pytest.mark.asyncio
    async def test_it_says_which_backends_can_host_e2(self, app_no_auth: Any) -> None:
        """The question that cost hours to answer from error messages alone."""
        async with _client(app_no_auth) as client:
            body = (await client.get("/capabilities")).json()

        hosts = {b["name"]: b["can_host_e2"] for b in body["browsers"]}
        assert hosts["camoufox"] is False  # Firefox: no CDP for browser-use
        assert hosts["patchright"] is True
        assert hosts["obscura"] is True

    @pytest.mark.asyncio
    async def test_it_reports_tier_availability(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            body = (await client.get("/capabilities")).json()

        tiers = {t["tier"]: t for t in body["tiers"]}
        assert set(tiers) == {"a_b_c", "d", "render", "e1", "e2"}
        assert tiers["a_b_c"]["available"] is True
        assert all(t["reason"] for t in body["tiers"])

    @pytest.mark.asyncio
    async def test_it_documents_the_tri_state_interactive_flag(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            body = (await client.get("/capabilities")).json()

        flag = body["flags"]["interactive"]
        assert flag["default"] is None
        assert "auto" in flag["description"]

    @pytest.mark.asyncio
    async def test_a_missing_agent_extra_marks_the_llm_tiers_unavailable(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the whole point: say so at startup, not on the first hard page."""
        monkeypatch.setattr(http_server, "_agent_available", lambda: False)

        async with _client(app_no_auth) as client:
            body = (await client.get("/capabilities")).json()

        tiers = {t["tier"]: t["available"] for t in body["tiers"]}
        assert tiers["e1"] is False
        assert tiers["e2"] is False
        assert tiers["render"] is False


class TestNoCrossTestImports:
    """Guard against the import that broke CI on the 3.2.0 release push.

    ``tests`` has no ``__init__.py``, so ``from tests.unit.x import y`` resolves
    only when the repository root is on ``sys.path``. ``python -m pytest``
    prepends the CWD and it works; the ``uv run pytest`` console script CI uses
    does not, and it fails at collection with ``No module named 'tests'``.

    A local run therefore cannot catch it, which is exactly why this guard is
    cheaper than remembering. It is the same kind of check the repo already uses
    to police hand-written tool lists.
    """

    def test_no_test_module_imports_the_tests_package(self) -> None:
        # Derived from __file__, not the CWD: this guard must hold wherever
        # pytest is invoked from, which is half the point of it.
        tests_root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in sorted(tests_root.rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith(("from tests.", "from tests ", "import tests")):
                    offenders.append(f"{path}:{lineno}: {stripped}")
        assert not offenders, (
            "test modules must not import each other via the `tests` package "
            "(it has no __init__.py, so this passes locally and fails in CI): "
            + "; ".join(offenders)
        )
