"""Cookie model and conversions — the pure half of the cookie feature.

Everything here is deterministic: build a cookie, match it against a URL,
convert it to whatever shape a tier wants, redact it for logging, read and write
the on-disk jar. The one genuinely untestable operation — reading a live browser
cookie store, which needs an OS credential store — lives in
:mod:`scrapper_tool._browser_cookies` and nothing in this module imports it.

That split is deliberate and load-bearing. Domain matching, key mapping, merge
ordering, expiry, redaction and file permissions are exactly the parts where a
bug is a security bug, and all of them are hermetically testable here.

The canonical type
------------------
**A Playwright cookie dict is the one canonical shape.** Harvest is identity
(``RenderResult.cookies`` already *is* this shape), injection is identity
(``BrowserContext.add_cookies`` consumes exactly it), and the two derivations —
``storage_state`` and a ``Cookie:`` header — are one-way-easy from it.

Field names stay snake_case on our own API, matching rookiepy and the rest of
this codebase; the camelCase conversion happens inside :func:`to_playwright`.
Exposing camelCase would make these the only camelCase fields in the whole
OpenAPI spec.

Why ``SecretStr``
-----------------
``value`` is a ``SecretStr`` because that gives structural redaction rather than
a convention someone has to remember:

* ``_logging`` renders values with ``repr``, and ``SecretStr.__repr__`` is
  ``SecretStr('**********')`` — the stdlib logging path is redacted for free.
* Pydantic serializes it masked, so an accidental echo-back in a response body
  is safe by construction.
* Reading the real value requires ``.get_secret_value()``, which makes every
  genuine read greppable. That is the audit property we want.

None of that removes the need for :func:`redact` at log sites — belt and braces,
because the two logging backends this project supports have different pipelines.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

__all__ = [
    "CookieIn",
    "SameSite",
    "cookie_jar_dir",
    "cookies_for_url",
    "domain_matches",
    "is_expired",
    "jar_path_for_domain",
    "load_cookies",
    "merge",
    "normalize_domain",
    "redact",
    "save_cookies",
    "to_cookie_header",
    "to_netscape",
    "to_playwright",
    "to_storage_state",
    "validate_domain_arg",
]

SameSite = Literal["Strict", "Lax", "None"]

#: A hostname needs at least "name.tld" — one label is a bare TLD or a typo.
_MIN_DOMAIN_LABELS = 2

#: Directory mode for the jar directory, and file mode for each jar. A cookie
#: jar is a credential store; group- and world-readable are both wrong.
_DIR_MODE = 0o700
_FILE_MODE = 0o600


class CookieIn(BaseModel):
    """One cookie, in the project's canonical snake_case shape."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    value: SecretStr
    domain: str = Field(min_length=1)
    path: str = "/"
    #: Epoch seconds. ``None`` means a session cookie — it dies with the browser
    #: and cannot be meaningfully persisted, but we still carry it because it is
    #: often the actual session token.
    expires: float | None = None
    http_only: bool = False
    secure: bool = True
    same_site: SameSite | None = None

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, raw: str) -> str:
        return normalize_domain(raw)

    @field_validator("path")
    @classmethod
    def _default_path(cls, raw: str) -> str:
        return raw or "/"


# ---------------------------------------------------------------------------
# Domain handling
# ---------------------------------------------------------------------------


def normalize_domain(raw: str) -> str:
    """Lowercase, strip a leading dot, strip any port.

    A leading dot is the pre-RFC-6265 way of writing "and subdomains"; modern
    parsers treat ``.example.com`` and ``example.com`` identically for matching
    purposes, so we normalize on the way in and let :func:`domain_matches` own
    the subdomain rule.
    """
    host = raw.strip().lower()
    if host.startswith("."):
        host = host[1:]
    # Strip a port if one was pasted in. Guard on the rsplit result being
    # numeric so an IPv6 literal isn't mangled.
    if ":" in host and not host.startswith("["):
        head, _, tail = host.rpartition(":")
        if head and tail.isdigit():
            host = head
    return host


def domain_matches(cookie_domain: str, host: str) -> bool:
    """RFC 6265 domain-match: exact host, or a dot-anchored suffix.

    The case this function exists to get right::

        domain_matches("example.com", "sub.example.com")   -> True
        domain_matches("example.com", "example.com")       -> True
        domain_matches("example.com", "evil-example.com")  -> False   # <-- the one
        domain_matches("example.com", "notexample.com")    -> False

    A naive ``host.endswith(cookie_domain)`` returns True for
    ``evil-example.com``, which hands an attacker's host every cookie scoped to
    the real domain. The suffix must be preceded by a literal dot.
    """
    cookie_domain = normalize_domain(cookie_domain)
    host = normalize_domain(host)
    if not cookie_domain or not host:
        return False
    if host == cookie_domain:
        return True
    return host.endswith("." + cookie_domain)


def path_matches(cookie_path: str, request_path: str) -> bool:
    """RFC 6265 path-match."""
    cookie_path = cookie_path or "/"
    request_path = request_path or "/"
    if cookie_path == request_path:
        return True
    if not request_path.startswith(cookie_path):
        return False
    # "/foo" must not match "/foobar", but must match "/foo/bar" and "/foo".
    return cookie_path.endswith("/") or request_path[len(cookie_path)] == "/"


def validate_domain_arg(raw: str) -> str:
    """Validate a user-supplied ``--domain``, returning the normalized host.

    Raises ``ValueError`` with an actionable message. Rejects wildcards and
    anything without a dot.

    Honest limitation: without a public-suffix list we cannot reliably tell
    ``co.uk`` (a public suffix, and a catastrophic thing to scope cookies to)
    from ``example.com``. This project deliberately has no PSL dependency —
    ``recipe.derive.registrable_domain`` documents the same trade-off, and it is
    only ever used as a cache key. So this check rejects the obvious mistakes
    and does not pretend to be a public-suffix implementation.
    """
    host = normalize_domain(raw)
    if not host:
        msg = "--domain must not be empty"
        raise ValueError(msg)
    if "*" in host:
        msg = f"--domain must be a concrete host, not a wildcard: {raw!r}"
        raise ValueError(msg)
    if "/" in host:
        msg = f"--domain must be a hostname, not a URL: {raw!r}"
        raise ValueError(msg)
    labels = host.split(".")
    if len(labels) < _MIN_DOMAIN_LABELS:
        msg = f"--domain must be a fully-qualified host such as app.example.com, got {raw!r}"
        raise ValueError(msg)
    if any(not label for label in labels):
        msg = f"--domain has an empty label: {raw!r}"
        raise ValueError(msg)
    return host


# ---------------------------------------------------------------------------
# Expiry and selection
# ---------------------------------------------------------------------------


def is_expired(cookie: CookieIn, *, now: float | None = None) -> bool:
    """True when ``cookie`` has a past expiry. Session cookies never count as expired."""
    if cookie.expires is None:
        return False
    return cookie.expires <= (time.time() if now is None else now)


def cookies_for_url(
    cookies: list[CookieIn], url: str, *, now: float | None = None
) -> list[CookieIn]:
    """Select the cookies that may legitimately be sent to ``url``.

    Applies domain-match, path-match, the ``secure`` flag, and expiry. This is
    the function that decides what leaves the process, so it errs toward sending
    nothing: an unparseable URL yields an empty list rather than everything.
    """
    parts = urlsplit(url)
    host = normalize_domain(parts.hostname or "")
    if not host:
        return []
    is_https = parts.scheme == "https"
    request_path = parts.path or "/"

    selected: list[CookieIn] = []
    for cookie in cookies:
        if not domain_matches(cookie.domain, host):
            continue
        if not path_matches(cookie.path, request_path):
            continue
        if cookie.secure and not is_https:
            continue
        if is_expired(cookie, now=now):
            continue
        selected.append(cookie)
    return selected


def merge(existing: list[CookieIn], incoming: list[CookieIn]) -> list[CookieIn]:
    """Merge two jars on ``(name, domain, path)``; later wins. Drops expired.

    Used both to fold a caller's cookies into harvested ones and to combine
    several browser profiles. Identity is the RFC's cookie identity — two
    cookies with the same name on different paths are genuinely different
    cookies, and collapsing them loses a real distinction.
    """
    merged: dict[tuple[str, str, str], CookieIn] = {}
    for cookie in [*existing, *incoming]:
        if is_expired(cookie):
            continue
        merged[(cookie.name, cookie.domain, cookie.path)] = cookie
    return list(merged.values())


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def to_playwright(cookies: list[CookieIn]) -> list[dict[str, Any]]:
    """Convert to Playwright's ``add_cookies`` shape (camelCase keys).

    Consumed as-is by ``BrowserContext.add_cookies``, by Scrapling's
    ``cookies=`` kwarg (which calls ``add_cookies`` internally), and by
    Crawl4AI's ``BrowserConfig(cookies=...)``.
    """
    out: list[dict[str, Any]] = []
    for cookie in cookies:
        entry: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value.get_secret_value(),
            "domain": cookie.domain,
            "path": cookie.path,
            "httpOnly": cookie.http_only,
            "secure": cookie.secure,
        }
        if cookie.expires is not None:
            entry["expires"] = cookie.expires
        if cookie.same_site is not None:
            entry["sameSite"] = cookie.same_site
        out.append(entry)
    return out


def to_storage_state(cookies: list[CookieIn]) -> dict[str, Any]:
    """Convert to a Playwright ``storage_state`` mapping.

    ``origins`` is always empty: it carries ``localStorage``, and **no tier in
    this codebase reads localStorage**. Emitting an empty list keeps the shape
    valid for consumers (browser-use 0.13 accepts ``storage_state`` on
    ``BrowserProfile``/``BrowserSession``) without implying we captured
    something we did not.
    """
    return {"cookies": to_playwright(cookies), "origins": []}


def to_cookie_header(cookies: list[CookieIn]) -> str:
    """Render a ``Cookie:`` request-header value.

    Note for callers: a static header is **not** safe to hand to a redirect-
    following client, because it is re-sent verbatim across a cross-domain
    redirect. Prefer setting a real cookie jar, which does domain scoping.
    """
    return "; ".join(f"{c.name}={c.value.get_secret_value()}" for c in cookies)


def to_netscape(cookies: list[CookieIn], *, host: str | None = None) -> str:
    """Render the Netscape ``cookies.txt`` format (curl / wget compatible).

    ``host``, when given, is the host the export was scoped to. It decides the
    include-subdomains flag: a cookie whose domain is a strict parent of ``host``
    was genuinely a subdomain-spanning cookie, and anything else is treated as
    host-only.

    Getting that flag wrong widens scope in a file we hand to external tools.
    Every entry used to be written as ``.<domain>`` + ``TRUE``, so a host-only
    cookie for ``app.example.com`` was exported as one curl would also send to
    ``other.app.example.com``.
    """
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by scrapper-tool. Treat this file as a credential.",
    ]
    for cookie in cookies:
        # A leading dot plus TRUE means "include subdomains" in this format.
        include_subdomains = host is not None and normalize_domain(host) != cookie.domain
        domain_field = ("." + cookie.domain) if include_subdomains else cookie.domain
        lines.append(
            "\t".join(
                [
                    domain_field,
                    "TRUE" if include_subdomains else "FALSE",
                    cookie.path,
                    "TRUE" if cookie.secure else "FALSE",
                    str(int(cookie.expires)) if cookie.expires is not None else "0",
                    cookie.name,
                    cookie.value.get_secret_value(),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def redact(cookies: list[CookieIn]) -> list[dict[str, Any]]:
    """Metadata-only view, safe to log or echo back.

    Every field except ``value`` survives, because names, domains and expiries
    are what make a cookie problem debuggable — and the value is the only part
    that is a credential.
    """
    return [
        {
            "name": c.name,
            "domain": c.domain,
            "path": c.path,
            "expires": c.expires,
            "secure": c.secure,
            "http_only": c.http_only,
            "same_site": c.same_site,
            "value_len": len(c.value.get_secret_value()),
        }
        for c in cookies
    ]


# ---------------------------------------------------------------------------
# On-disk jar
# ---------------------------------------------------------------------------


def cookie_jar_dir() -> Path:
    """``$SCRAPPER_TOOL_COOKIE_DIR`` or ``~/.scrapper-tool/cookies``."""
    override = os.environ.get("SCRAPPER_TOOL_COOKIE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".scrapper-tool" / "cookies"


def jar_path_for_domain(domain: str, *, directory: Path | None = None) -> Path:
    """Path of the jar file for ``domain``."""
    base = directory if directory is not None else cookie_jar_dir()
    return base / f"{normalize_domain(domain)}.json"


def save_cookies(
    cookies: list[CookieIn],
    domain: str,
    *,
    directory: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Write a jar to disk at ``0600`` inside a ``0700`` directory.

    Written via ``os.open(..., O_CREAT | O_EXCL, 0o600)`` so the file is created
    with restrictive permissions from the first byte, rather than created world-
    readable and chmod'd afterwards — that gap is a real window on a shared box.
    ``O_EXCL`` also means we never silently widen or clobber an existing file;
    ``overwrite=True`` replaces it explicitly, still via a fresh exclusive
    create.
    """
    base = directory if directory is not None else cookie_jar_dir()
    base.mkdir(parents=True, exist_ok=True)
    _harden_dir(base)

    target = jar_path_for_domain(domain, directory=base)
    payload = json.dumps(
        {"domain": normalize_domain(domain), "cookies": to_playwright(cookies)},
        indent=2,
    )

    if overwrite and target.exists():
        target.unlink()

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(target, flags, _FILE_MODE)
    except FileExistsError as exc:
        msg = f"{target} already exists — pass --force to replace it"
        raise FileExistsError(msg) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return target


def load_cookies(domain: str, *, directory: Path | None = None) -> list[CookieIn]:
    """Read a previously exported jar. Returns ``[]`` when none exists.

    This is the function the documented usage calls::

        result = await scrape(url, cookies=load_cookies("app.example.com"))
    """
    target = jar_path_for_domain(domain, directory=directory)
    if not target.is_file():
        return []
    text = target.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        # Say which file and what shape it should be. A bare JSONDecodeError
        # pointing at "line 1 column 1" is a genuinely baffling thing to get
        # back from a function you called as `load_cookies("example.com")`.
        msg = (
            f"{target} is not a scrapper-tool cookie jar (invalid JSON: {exc}). "
            "Jars are written by `scrapper-tool cookies export --format json`; "
            "the netscape and header formats are for external tools and are "
            "written to their own filenames."
        )
        raise ValueError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"{target} is not a scrapper-tool cookie jar (expected a JSON object)"
        raise ValueError(msg)
    return from_playwright(raw.get("cookies", []))


def from_browser_store(raw: list[dict[str, Any]]) -> list[CookieIn]:
    """Normalize raw rows from a browser cookie store into the canonical model.

    Lives here, not in the browser-store shim, on purpose: this is *logic*, and
    logic belongs in the module that has tests. The shim stays two lines of
    genuinely untestable I/O.

    Backends disagree on spelling — rookiepy emits snake_case (``http_only``,
    ``same_site``) while anything Playwright-flavoured emits camelCase — so both
    are accepted. Rows missing a name or value are skipped rather than raising:
    a single malformed row in a browser's SQLite should not fail an export the
    user is watching.
    """
    out: list[CookieIn] = []
    for entry in raw:
        name = entry.get("name")
        value = entry.get("value")
        domain = entry.get("domain")
        if not name or value is None or not domain:
            continue
        same_site = entry.get("same_site", entry.get("sameSite"))
        if isinstance(same_site, str):
            # Stores spell this 'lax'/'strict'/'no_restriction'; the canonical
            # model uses Playwright's capitalised triple.
            normalized = same_site.strip().lower()
            same_site = {
                "lax": "Lax",
                "strict": "Strict",
                "none": "None",
                "no_restriction": "None",
                "unspecified": None,
            }.get(normalized)
        else:
            same_site = None
        out.append(
            CookieIn(
                name=str(name),
                value=SecretStr(str(value)),
                domain=str(domain),
                path=str(entry.get("path") or "/"),
                expires=_coerce_expires(entry.get("expires")),
                http_only=bool(entry.get("http_only", entry.get("httpOnly", False))),
                secure=bool(entry.get("secure", True)),
                same_site=same_site,
            )
        )
    return out


def _coerce_expires(raw: Any) -> float | None:
    """Coerce a store's expiry to epoch seconds, or None for a session cookie."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # Several stores use 0 to mean "session cookie" rather than "1970".
    return None if value <= 0 else value


def from_playwright(raw: list[dict[str, Any]]) -> list[CookieIn]:
    """Parse Playwright-shaped dicts back into the canonical model."""
    out: list[CookieIn] = []
    for entry in raw:
        out.append(
            CookieIn(
                name=entry["name"],
                value=SecretStr(entry["value"]),
                domain=entry["domain"],
                path=entry.get("path", "/"),
                expires=entry.get("expires"),
                http_only=bool(entry.get("httpOnly", False)),
                secure=bool(entry.get("secure", True)),
                same_site=entry.get("sameSite"),
            )
        )
    return out


def _harden_dir(path: Path) -> None:
    """Best-effort ``0700`` on the jar directory.

    On Windows the POSIX mode bits are advisory, so this can succeed while
    granting nothing. Callers that care should warn rather than claim the
    directory is protected — see the CLI, which does exactly that.

    The guard tests ``os.name``, not ``sys.platform``, on purpose: mypy
    special-cases ``sys.platform`` and narrows it to the *checking* host, so on
    a Windows machine it proves the rest of this function is dead and
    ``--strict`` fails with ``[unreachable]``. ``os.name`` gets no such
    treatment, so the same source type-checks on every platform.
    """
    if os.name == "nt":  # pragma: no cover — POSIX-only CI
        return
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current != _DIR_MODE:
            path.chmod(_DIR_MODE)
    except OSError:
        return
