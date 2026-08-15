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
from scrapper_tool.errors import AgentLLMError, ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scrapper_tool.agent.types import AgentConfig

_logger = get_logger(__name__)


# Reasoning models spend budget before they emit any content: measured on
# ``google/gemma-4-e4b``, a 20-token budget returned ``content: ""`` with
# ``finish_reason: "length"`` because the entire allowance went to
# ``reasoning_content``; at 400 it answered immediately. A solver that caps
# tokens low to get a terse "tiles: 1,3,5" would read that starvation as solver
# failure, so the floor is deliberately generous.
_VISION_MAX_TOKENS = 512
# Local VLMs on CPU/modest GPUs are slow; a grid solve must not time out mid-think.
_VISION_TIMEOUT_S = 120.0


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

    async def complete_vision(
        self,
        prompt: str,
        images_b64: Sequence[str],
        *,
        max_tokens: int = _VISION_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> str:
        """Send ``prompt`` plus base64 PNG/JPEG images, return the text reply.

        The direct inference path the local captcha solvers need. Everything
        else on this protocol hands the model to another framework
        (browser-use, Crawl4AI); nothing could simply ask a question about an
        image, which is exactly what a grid or OCR solver does.

        Raises :class:`AgentLLMError` on transport failure or an empty reply.
        """


def _extract_message_text(payload: Any) -> str:
    """Pull assistant text out of an OpenAI-compatible chat completion.

    Falls back to ``reasoning_content`` when ``content`` is empty: that is the
    shape a reasoning model returns when it ran out of budget mid-thought, and
    surfacing the partial reasoning gives a far better error than "".
    """
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    # Some servers return content as a list of parts.
    if isinstance(content, list):
        joined = "".join(part.get("text", "") for part in content if isinstance(part, dict)).strip()
        if joined:
            return joined
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        _logger.warning(
            "agent.llm.vision_only_reasoning",
            detail="model emitted reasoning but no content; raise max_tokens",
            finish_reason=choices[0].get("finish_reason"),
        )
        return reasoning
    return ""


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

    async def complete_vision(
        self,
        prompt: str,
        images_b64: Sequence[str],
        *,
        max_tokens: int = _VISION_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> str:
        """Ollama's native ``/api/chat`` takes images as a sibling base64 list."""
        url = urljoin(self.base_url + "/", "api/chat")
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt, "images": list(images_b64)}],
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        try:
            async with httpx.AsyncClient(timeout=_VISION_TIMEOUT_S) as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            msg = f"Ollama vision call failed at {self.base_url}: {exc}"
            raise AgentLLMError(msg) from exc
        if resp.status_code >= 400:  # noqa: PLR2004 — HTTP error threshold
            msg = f"Ollama vision call returned HTTP {resp.status_code}: {resp.text[:200]}"
            raise AgentLLMError(msg)
        try:
            text = str((resp.json().get("message") or {}).get("content") or "")
        except (ValueError, AttributeError) as exc:
            msg = "Ollama vision call returned an unexpected shape"
            raise AgentLLMError(msg) from exc
        if not text.strip():
            raise AgentLLMError("Ollama vision call returned an empty reply")
        return text


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

        # Best-effort model check. Skip when the server returns an empty list
        # (some providers omit the catalogue entirely).
        try:
            data = resp.json()
            ids = {m.get("id", "") for m in data.get("data", [])}
            if ids and self.model not in ids and not any(self.model in n for n in ids):
                available = ", ".join(sorted(ids))
                msg = f"Model {self.model!r} not listed by /v1/models. Available: {available}"
                raise AgentLLMError(msg)
        except (ValueError, AttributeError):
            pass  # non-JSON or unexpected shape — skip model check

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

    async def complete_vision(
        self,
        prompt: str,
        images_b64: Sequence[str],
        *,
        max_tokens: int = _VISION_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> str:
        """OpenAI-style multimodal content parts — what LM Studio and vLLM expect."""
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}}
            for img in images_b64
        )
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = urljoin(self.base_url + "/", "v1/chat/completions")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            async with httpx.AsyncClient(timeout=_VISION_TIMEOUT_S, headers=headers) as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            msg = f"Vision call failed at {self.base_url}: {exc}"
            raise AgentLLMError(msg) from exc
        if resp.status_code >= 400:  # noqa: PLR2004 — HTTP error threshold
            msg = f"Vision call returned HTTP {resp.status_code}: {resp.text[:200]}"
            raise AgentLLMError(msg)
        try:
            text = _extract_message_text(resp.json())
        except ValueError as exc:
            msg = "Vision call returned non-JSON"
            raise AgentLLMError(msg) from exc
        if not text.strip():
            raise AgentLLMError(
                "Vision call returned an empty reply "
                f"(model={self.model!r}; if it is a reasoning model, raise max_tokens)"
            )
        return text


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
        api_key = config.llm_api_key.get_secret_value() if config.llm_api_key else None
        return cls(model=config.model, base_url=config.ollama_url, api_key=api_key)
    msg = f"Unknown LLM backend: {config.llm!r}"
    raise ConfigurationError(msg)


# Name fragments that imply a multimodal model. Used only when the server cannot
# be asked (see `supports_vision`) — naming is genuinely unreliable, which is the
# whole reason this is now the fallback rather than the answer.
_VISION_NAME_TAGS: tuple[str, ...] = (
    "vl",  # qwen2-vl, internvl, smolvlm, cogvlm
    "vision",  # llama-3.2-vision, phi-3.5-vision
    "llava",
    "minicpm-v",
    "gemma-3",  # Gemma 3 and 4 are multimodal; Gemma 2 is not
    "gemma-4",
    "pixtral",
    "moondream",
    "idefics",
    "internvl",
    "multimodal",
)


def is_vision_model(model: str) -> bool:
    """Name-only heuristic for whether ``model`` accepts image input.

    **Prefer :func:`supports_vision`**, which asks the server. This is the offline
    fallback and it is known-lossy: it answered False for both ``google/gemma-4-e4b``
    and ``qwen/qwen3.6-27b`` while LM Studio reported both as ``type=vlm`` — so
    browse mode disabled vision on models that demonstrably see, and E2 ran blind.
    Vendors simply do not encode modality in model names consistently.
    """
    needle = model.lower()
    return any(tag in needle for tag in _VISION_NAME_TAGS)


async def supports_vision(model: str, base_url: str | None = None) -> bool:
    """Whether ``model`` accepts image input, asking the server when possible.

    LM Studio's ``/api/v0/models`` reports an explicit ``type`` per model
    (``vlm`` / ``llm`` / ``embeddings``), which is authoritative and costs one
    local request. Anything else — endpoint absent (plain llama.cpp, vLLM, Ollama),
    unreachable, or the model simply not listed — falls back to
    :func:`is_vision_model`, so this is never worse than the old behaviour.
    """
    if not base_url:
        return is_vision_model(model)
    url = urljoin(base_url.rstrip("/") + "/", "api/v0/models")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:  # noqa: PLR2004 — HTTP error threshold
            return is_vision_model(model)
        entries = resp.json().get("data", [])
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        _logger.debug("agent.llm.vision_probe_failed", model=model, error=str(exc))
        return is_vision_model(model)

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == model:
            declared = str(entry.get("type", "")).lower()
            if declared:
                _logger.info("agent.llm.vision_probe_ok", model=model, declared_type=declared)
                return declared == "vlm"
            break
    return is_vision_model(model)


async def get_vision_backend(config: AgentConfig) -> LLMBackend | None:
    """The backend for the captcha image-grid tier, or ``None`` if it cannot see.

    Honours ``captcha_vision_model`` when set, because extraction and captcha
    solving want opposite things from a model — see the field's docstring. Falls
    back to the main ``model``, and returns ``None`` when whichever model applies
    has no vision, so the grid tier is skipped rather than handed an image it
    cannot read.
    """
    wanted = config.captcha_vision_model or config.model
    if not await supports_vision(wanted, config.ollama_url):
        return None
    if wanted == config.model:
        return get_llm_backend(config)
    return get_llm_backend(config.model_copy(update={"model": wanted}))


__all__ = [
    "LLMBackend",
    "LlamaCppBackend",
    "OllamaBackend",
    "OpenAICompatBackend",
    "VLLMBackend",
    "get_llm_backend",
    "get_vision_backend",
    "is_vision_model",
    "supports_vision",
]
