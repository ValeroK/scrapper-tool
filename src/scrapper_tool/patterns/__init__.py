"""Pattern A/B/C/D extraction helpers.

See ``docs/recon.md`` for the pattern decision tree:

- ``a`` — JSON API (preferred). Helpers in ``patterns.a``.
- ``b`` — Embedded JSON (LD+JSON / ``__NEXT_DATA__`` / ``__NUXT__`` / ``self.__next_f.push``). ``patterns.b`` (extruct-backed).
- ``c`` — CSS / schema.org microdata. ``patterns.c`` (selectolax-backed).
- ``d`` — Hostile (Cloudflare Turnstile / Akamai EVA / etc.). ``patterns.d`` (Scrapling-backed; ``pip install scrapper-tool[hostile]``).

Submodules are populated incrementally by milestones M3/M4/M5.
"""

from __future__ import annotations
