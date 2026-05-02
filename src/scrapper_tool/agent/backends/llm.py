"""LLM backend abstractions for Pattern E.

Two responsibilities:

1. ``probe()`` — verify the LLM is reachable + the requested model is
   available, raising :class:`scrapper_tool.errors.AgentLLMError`
   early so failures surface at session start, not mid-run.
2. Adapter helpers (``to_browser_use_llm``, ``to_crawl4ai_provider``)
   that produce framework-compatible objects without leaking the
   framework imports into module-import-time.

Default = :class:`OllamaBackend`. The other backends (llama.cpp via
OpenAI-compat shim, vLLM, generic OpenAI-compat) all share the same
HTTP probe logic; only the framework adapter shapes differ.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urljoin

import httpx

from scrapper_tool._logging import get_logger
from scrapper_tool.errors import AgentLLMError

if TYPE_CHECKING:
    from scrapper_tool.agent.types import AgentConfig

_logger = get_logger(__name__)


class LLMBackend(Protocol):
    """Protocol implemented by all LLM backends."""

    name: str
    model: str

    async def probe(self) -> None:
        """Verify the backend is reachable and ``model`` is available.

        Raises :class:`AgentLLMError` on failure.
        """

    def to_browser_use_llm(self) -> Any:
        """Return a langchain-style chat object suitable for browser-use.

        Lazy-imports the framework so this module loads without the
        framework installed.
        """

    def to_crawl4ai_provider(self) -> tuple[str, str | None, str | None]:
        """Return ``(provider, api_base, api_token)`` for Crawl4AI.

        ``provider`` is a litellm-style identifier such as
        ``"ollama/qwen3-vl:8b"`` or ``"openai/gpt-4o"``.
        """


# --- Ollama (default) ----------------------------------------------------


class OllamaBackend:
    """Local Ollama backend — default for the "free + local" goal."""

    name = "ollama"

    def __init__(self, *, model: str, base_url: str = "http://localhost:11434") -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def probe(self) -> None:
        url = urljoin(self.base_url + "/", "api/tags")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            msg = f"Ollama unreachable at {self.base_url}: {exc}"
            raise AgentLLMError(msg) from exc

        if resp.status_code >= 400:  # noqa: PLR2004 — HTTP error threshold
            msg = f"Ollama probe returned HTTP {resp.status_code} from {url}"
            raise AgentLLMError(msg)

        try:
            payload = resp.json()
        except ValueError as exc:
            msg = f"Ollama returned non-JSON from {url}"
            raise AgentLLMError(msg) from exc

        models = {m.get("name") for m in payload.get("models", [])}
        # Ollama tag names can be ``qwen2.5-vl:7b`` or just ``qwen2.5-vl``;
        # accept either as a hit.
        wanted = self.model
        wanted_base = wanted.split(":")[0]
        if wanted not in models and wanted_base not in {m.split(":")[0] for m in models if m}:
            available = ", ".join(sorted(models)) or "(none — no models pulled)"
            msg = (
                f"Ollama model {wanted!r} not pulled. Available: {available}. "
                f"Pull with: ollama pull {wanted}"
            )
            raise AgentLLMError(msg)

        _logger.info("agent.llm.ollama.probe_ok", model=wanted, base_url=self.base_url)

    def to_browser_use_llm(self) -> Any:
        # browser-use 0.5+ requires its own LLM wrapper instead of accepting
        # a generic langchain chat object. ChatOllama is bundled with the
        # ``[llm-agent]`` extra (transitively via browser-use).
        try:
            from browser_use.llm.ollama.chat import ChatOllama  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — covered by unit mock
            msg = "browser-use not installed. pip install scrapper-tool[llm-agent]"
            raise AgentLLMError(msg) from exc
        return ChatOllama(model=self.model, host=self.base_url)

    def to_crawl4ai_provider(self) -> tuple[str, str | None, str | None]:
        return f"ollama/{self.model}", self.base_url, None


# --- OpenAI-compat (covers llama.cpp, vLLM, LM Studio, …) ----------------


class OpenAICompatBackend:
    """Generic OpenAI-compatible HTTP backend.

    Works with any server that implements the ``/v1/chat/completions``
    endpoint — llama.cpp's server mode, vLLM, LM Studio, Tabby, etc.
    """

    name = "openai_compat"

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def probe(self) -> None:
        url = urljoin(self.base_url + "/", "v1/models")
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
                resp = await client.get(url)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            msg = f"OpenAI-compat server unreachable at {self.base_url}: {exc}"
            raise AgentLLMError(msg) from exc

        if resp.status_code >= 400:  # noqa: PLR2004 — HTTP error threshold
            msg = f"OpenAI-compat probe returned HTTP {resp.status_code} from {url}"
            raise AgentLLMError(msg)
        _logger.info("agent.llm.openai_compat.probe_ok", model=self.model)

    def to_browser_use_llm(self) -> Any:
        # browser-use 0.5+ ships a native ChatOpenAI; we point its
        # ``base_url`` at LM Studio / vLLM / llama.cpp / any remote
        # OpenAI-compat endpoint. The ``model`` parameter has a Literal
        # type-hint enumerating OpenAI's official names but accepts any
        # str at runtime — that's how custom local models work.
        try:
            from browser_use.llm.openai.chat import ChatOpenAI  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — covered by unit mock
            msg = "browser-use not installed. pip install scrapper-tool[llm-agent]"
            raise AgentLLMError(msg) from exc
        # `model` has a Literal type hint enumerating OpenAI's official
        # names but accepts any str at runtime — that's how custom local
        # models work.
        return ChatOpenAI(
            model=self.model,
            base_url=self.base_url + "/v1",
            api_key=self.api_key or "no-key-needed",
        )

    def to_crawl4ai_provider(self) -> tuple[str, str | None, str | None]:
        return f"openai/{self.model}", self.base_url + "/v1", self.api_key


class LlamaCppBackend(OpenAICompatBackend):
    """llama.cpp ``server`` is OpenAI-compatible — alias for clarity."""

    name = "llama_cpp"


class VLLMBackend(OpenAICompatBackend):
    """vLLM is OpenAI-compatible — alias for clarity."""

    name = "vllm"


# --- Resolver -------------------------------------------------------------


def get_llm_backend(config: AgentConfig) -> LLMBackend:
    """Build an LLM backend from config."""
    if config.llm == "ollama":
        return OllamaBackend(model=config.model, base_url=config.ollama_url)
    # llama.cpp / vLLM / generic OpenAI-compat all use the same probe.
    if config.llm in {"openai_compat", "llama_cpp", "vllm"}:
        # ``ollama_url`` doubles as the base URL when llm≠ollama —
        # keeping config flat avoids a separate field for every backend.
        cls: type[OpenAICompatBackend] = {
            "openai_compat": OpenAICompatBackend,
            "llama_cpp": LlamaCppBackend,
            "vllm": VLLMBackend,
        }[config.llm]
        return cls(model=config.model, base_url=config.ollama_url)
    msg = f"Unknown LLM backend: {config.llm!r}"
    raise ValueError(msg)


def is_vision_model(model: str) -> bool:
    """Heuristic — used by browse mode to enable/disable vision input.

    Saves tokens when running text-only models like Qwen3-Coder.
    """
    needle = model.lower()
    return any(tag in needle for tag in ("vl", "vision", "llava", "minicpm-v"))


__all__ = [
    "LLMBackend",
    "LlamaCppBackend",
    "OllamaBackend",
    "OpenAICompatBackend",
    "VLLMBackend",
    "get_llm_backend",
    "is_vision_model",
]
