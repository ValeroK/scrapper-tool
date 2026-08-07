"""``scrapper-tool cookies`` — domain-scoped browser-cookie extraction.

The whole point of this command is that **we never see a password**. The user
logs into the site in their own browser, normally; we read the resulting cookie
and hand it to the cascade. No automation drives a login form, no credential is
stored, and nothing but the cookie is persisted.

Two subcommands::

    scrapper-tool cookies export --domain app.example.com
    scrapper-tool cookies seed-profile --domain app.example.com --profile-dir ./p

Exit codes
----------

- ``0`` — cookies found and written.
- ``1`` — zero cookies matched. A real, actionable outcome ("you're not logged
  in, or the domain is wrong"), not an error.
- ``2`` — usage error, or the export was declined and nothing was written.
- ``3`` — no cookie backend available, or the backend refused to read.

A declined confirmation is deliberately **not** ``0``. Nothing was written, and
a script that checks the exit code and then reads the jar would otherwise find
a stale file or none at all while believing the export succeeded. The same
applies to the non-TTY refusal: a piped invocation without ``--yes`` writes
nothing, and must not claim success for it.

Deliberately not exposed over MCP
---------------------------------
An LLM agent that can silently dump the user's browser cookie store is exactly
the capability not to build, and a consent prompt is meaningless when the caller
is a model. MCP tools *consume* cookies passed to them; they never read the
browser. That asymmetry is stated in ``SKILL.md`` so agents don't try to shell
out to this CLI to route around it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scrapper_tool import cookies as cookies_mod
from scrapper_tool._browser_cookies import (
    BrowserCookieError,
    NoCookiesFound,
    read_browser_cookies,
)

if TYPE_CHECKING:
    import argparse

__all__ = ["add_subparser", "run_cli"]

_EXIT_OK = 0
_EXIT_NO_COOKIES = 1
_EXIT_USAGE = 2
_EXIT_NO_BACKEND = 3

#: A declined or non-confirmable export. Shares the usage code rather than
#: inventing a fourth: from a caller's point of view "you did not confirm" and
#: "you invoked this wrongly" are the same class of outcome — nothing was
#: written, and the fix is to re-run differently.
_EXIT_ABORTED = _EXIT_USAGE


def add_subparser(sub: Any) -> None:
    """Register the ``cookies`` subcommand and its own sub-subcommands."""
    cookies = sub.add_parser(
        "cookies",
        help="Export cookies for one domain from a local browser profile.",
        description=(
            "Reads cookies for a single domain out of a browser profile on this "
            "machine so the cascade can scrape a logged-in page. You log in "
            "normally in your own browser; this never sees a password. "
            "Extraction is host-side only - it needs the OS credential store, "
            "which a container does not have."
        ),
    )
    inner = cookies.add_subparsers(dest="cookies_command", required=True)

    export = inner.add_parser(
        "export",
        help="Export cookies for --domain to a jar file.",
        description="Export cookies for one domain. Values are never printed by default.",
    )
    _add_common_args(export)
    export.add_argument(
        "--out",
        type=str,
        default=None,
        help="Write here instead of the default jar (~/.scrapper-tool/cookies/).",
    )
    export.add_argument(
        "--format",
        choices=("json", "netscape", "header"),
        default="json",
        help="Output format. Default: json (the cascade's own shape).",
    )
    export.add_argument(
        "--print-values",
        action="store_true",
        help="Reveal cookie values in the table. Requires --yes.",
    )
    export.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing jar file instead of refusing.",
    )
    export.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (metadata only - never values).",
    )

    seed = inner.add_parser(
        "seed-profile",
        help="Write cookies into a Playwright storage_state.json for a profile dir.",
        description=(
            "Writes a Playwright storage_state.json that a browser tier can load. "
            "This is the recommended path for Docker: bind-mount the profile dir "
            "so cookie values never cross the HTTP boundary."
        ),
    )
    _add_common_args(seed)
    seed.add_argument(
        "--profile-dir",
        type=str,
        required=True,
        help=(
            "Directory to seed. Created 0700 if absent. Must not be $HOME "
            "or a live browser profile."
        ),
    )


def _add_common_args(parser: Any) -> None:
    parser.add_argument(
        "--domain",
        type=str,
        required=True,
        help="Host to scope the export to, e.g. app.example.com. No wildcards.",
    )
    parser.add_argument(
        "--browser",
        type=str,
        default=None,
        help="Read only this browser (firefox, chrome, ...). Default: try each in turn.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt. Required when stdin is not a TTY.",
    )


def run_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Handler invoked by the ``scrapper-tool`` dispatcher."""
    try:
        domain = cookies_mod.validate_domain_arg(args.domain)
    except ValueError as exc:
        parser.error(str(exc))

    if getattr(args, "print_values", False) and not args.yes:
        parser.error("--print-values requires --yes (it reveals credentials on your terminal)")

    if sys.platform.startswith("linux") and _in_container():
        sys.stderr.write(
            "cookies: reading a browser cookie store needs an OS credential store and a\n"
            "browser profile, neither of which exists in a container. Run this on your\n"
            "host, then pass the exported jar in (or bind-mount a seeded profile dir).\n"
        )
        return _EXIT_NO_BACKEND

    try:
        raw = read_browser_cookies(domain, browser=args.browser)
    except NoCookiesFound as exc:
        sys.stderr.write(_no_cookies_message(domain, exc.browsers_searched, args.browser))
        return _EXIT_NO_COOKIES
    except BrowserCookieError as exc:
        sys.stderr.write(f"cookies: {exc}\n")
        return _EXIT_NO_BACKEND

    found = cookies_mod.from_browser_store(raw)
    # The store hands back everything it matched; re-apply our own domain rule
    # so a backend's looser matching can't widen the export.
    #
    # One-directional on purpose. A cookie scoped to the parent (`example.com`)
    # *is* sent to `app.example.com`, so it belongs in an export for that host.
    # The reverse is not true: a cookie scoped to `sub.example.com` is never
    # sent to `example.com`, so including it when the user asked for the parent
    # would collect credentials the target host will never receive.
    found = [c for c in found if cookies_mod.domain_matches(c.domain, domain)]

    if not found:
        sys.stderr.write(_no_cookies_message(domain, [], args.browser))
        return _EXIT_NO_COOKIES

    if args.cookies_command == "seed-profile":
        return _run_seed_profile(args, found, domain)
    return _run_export(args, found, domain)


def _no_cookies_message(domain: str, searched: list[str], browser: str | None) -> str:
    """Explain an empty result, naming what was actually searched.

    "no cookies found" on its own sends people to check the domain when the far
    more common cause is that they are logged in on a browser we did not reach.
    Firefox leads the search order deliberately, so a Chrome user hits this
    constantly.
    """
    lines = [f"cookies: no cookies found for {domain}."]
    if browser:
        lines.append(f"Searched {browser} only. Drop --browser to try the others.")
    elif searched:
        lines.append(f"Searched: {', '.join(searched)}.")
        lines.append("If your session is in a different browser, pass --browser <name>.")
    lines.append("Otherwise: are you logged in, and is the domain right?")
    return "\n".join(lines) + "\n"


def _run_export(args: argparse.Namespace, found: list[Any], domain: str) -> int:
    if args.json:
        sys.stdout.write(
            json.dumps(
                {"domain": domain, "count": len(found), "cookies": cookies_mod.redact(found)},
                indent=2,
            )
        )
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_format_table(found, domain, reveal=args.print_values))
        sys.stdout.write("\n")

    destination = _export_target(args, domain)
    if not _confirm(f"Export these to {destination}?", assume_yes=args.yes):
        sys.stderr.write("cookies: aborted.\n")
        return _EXIT_ABORTED

    try:
        written = _write_export(found, domain, args)
    except FileExistsError as exc:
        sys.stderr.write(f"cookies: {exc}\n")
        return _EXIT_USAGE
    except OSError as exc:
        sys.stderr.write(f"cookies: could not write {destination}: {exc}\n")
        return _EXIT_USAGE

    sys.stdout.write(f"Wrote {len(found)} cookies to {written}\n")
    _warn_if_permissions_are_advisory()
    return _EXIT_OK


#: Filename suffix per ``--format``. Only ``json`` is the cascade's own jar
#: shape; the other two are for handing to external tools.
_FORMAT_SUFFIX = {"json": ".json", "netscape": ".cookies.txt", "header": ".cookie-header.txt"}


def _export_target(args: argparse.Namespace, domain: str) -> Path:
    """Where this export will be written.

    The default filename carries the format, which is not cosmetic. Previously
    every format with no ``--out`` landed on ``<domain>.json`` — the exact path
    :func:`~scrapper_tool.cookies.load_cookies` reads — so
    ``--format netscape`` wrote Netscape text into the JSON jar and the next
    ``load_cookies(domain)`` died on a ``JSONDecodeError``. An explicit
    ``--out`` is still honoured verbatim; the user named the file.
    """
    if args.out:
        return Path(args.out)
    jar = cookies_mod.jar_path_for_domain(domain)
    suffix = _FORMAT_SUFFIX.get(args.format, ".json")
    return jar if suffix == ".json" else jar.with_name(jar.stem + suffix)


def _write_export(found: list[Any], domain: str, args: argparse.Namespace) -> Path:
    if args.format == "json" and not args.out:
        return cookies_mod.save_cookies(found, domain, overwrite=args.force)

    target = _export_target(args, domain)
    if args.format == "netscape":
        payload = cookies_mod.to_netscape(found, host=domain)
    elif args.format == "header":
        payload = cookies_mod.to_cookie_header(found) + "\n"
    else:
        payload = json.dumps(
            {"domain": domain, "cookies": cookies_mod.to_playwright(found)}, indent=2
        )
    _write_secret_file(target, payload, force=args.force)
    return target


def _run_seed_profile(args: argparse.Namespace, found: list[Any], domain: str) -> int:
    profile_dir = Path(args.profile_dir).expanduser()
    try:
        _reject_dangerous_profile_dir(profile_dir)
    except ValueError as exc:
        sys.stderr.write(f"cookies: {exc}\n")
        return _EXIT_USAGE

    sys.stdout.write(_format_table(found, domain, reveal=False))
    sys.stdout.write("\n")
    if not _confirm(f"Seed {profile_dir} with these cookies?", assume_yes=args.yes):
        sys.stderr.write("cookies: aborted.\n")
        return _EXIT_ABORTED

    profile_dir.mkdir(parents=True, exist_ok=True)
    if not sys.platform.startswith("win"):
        profile_dir.chmod(0o700)

    target = profile_dir / "storage_state.json"
    payload = json.dumps(cookies_mod.to_storage_state(found), indent=2)
    try:
        _write_secret_file(target, payload, force=True)
    except OSError as exc:
        sys.stderr.write(f"cookies: could not write {target}: {exc}\n")
        return _EXIT_USAGE

    sys.stdout.write(f"Seeded {target} with {len(found)} cookies\n")
    sys.stdout.write(
        "Point a browser tier at it with persist_browser_profile_dir, or bind-mount\n"
        f"{profile_dir} into the container.\n"
    )
    _warn_if_permissions_are_advisory()
    return _EXIT_OK


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_table(found: list[Any], domain: str, *, reveal: bool) -> str:
    lines = [
        f"Domain:   {domain} (+ subdomains)",
        f"Found:    {len(found)} cookies",
        "",
        f"{'NAME':<16} {'DOMAIN':<24} {'PATH':<6} {'EXPIRES':<20} {'SEC':<4} {'HTTPONLY'}",
    ]
    for cookie in found:
        expires = "session" if cookie.expires is None else _format_epoch(cookie.expires)
        lines.append(
            f"{cookie.name[:16]:<16} {cookie.domain[:24]:<24} {cookie.path[:6]:<6} "
            f"{expires:<20} {'yes' if cookie.secure else 'no':<4} "
            f"{'yes' if cookie.http_only else 'no'}"
        )
        if reveal:
            lines.append(f"    value: {cookie.value.get_secret_value()}")
    if not reveal:
        lines.append("")
        lines.append("(values hidden - pass --print-values --yes to reveal)")
    return "\n".join(lines)


def _format_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%MZ")


def _confirm(question: str, *, assume_yes: bool) -> bool:
    """Prompt unless ``--yes``. A non-TTY without ``--yes`` is a refusal.

    Refusing rather than assuming yes is the safe default for a command whose
    output is a credential: a piped invocation should not silently export.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        sys.stderr.write("cookies: stdin is not a TTY - pass --yes to confirm.\n")
        return False
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _write_secret_file(target: Path, payload: str, *, force: bool) -> None:
    """Create ``target`` at 0600 without ever widening an existing file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if not sys.platform.startswith("win"):
        with contextlib.suppress(OSError):
            target.parent.chmod(0o700)
    if force and target.exists():
        target.unlink()
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)


def _reject_dangerous_profile_dir(path: Path) -> None:
    """Refuse $HOME, /, and anything that looks like a live browser profile.

    Writing into a browser's real profile directory while the browser owns it
    corrupts it, and the failure is silent until the next launch.
    """
    resolved = path.expanduser().resolve()
    if resolved == Path("/"):
        msg = "--profile-dir must not be /"
        raise ValueError(msg)
    if resolved == Path.home().resolve():
        msg = "--profile-dir must not be your home directory"
        raise ValueError(msg)

    live_markers = ("cookies.sqlite", "Cookies", "places.sqlite", "Login Data")
    if any((resolved / marker).exists() for marker in live_markers):
        msg = (
            f"{resolved} looks like a live browser profile - writing into it can "
            "corrupt the profile. Use a fresh directory."
        )
        raise ValueError(msg)


def _warn_if_permissions_are_advisory() -> None:
    if sys.platform.startswith("win"):  # pragma: no cover — POSIX-only CI
        sys.stderr.write(
            "cookies: warning - on Windows the 0600 mode bits are advisory. "
            "Protect this file with NTFS ACLs.\n"
        )


def _in_container() -> bool:
    """Best-effort container detection, used only to print a better message."""
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
