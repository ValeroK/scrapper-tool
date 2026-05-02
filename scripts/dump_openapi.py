"""Dump the FastAPI OpenAPI 3.1 spec for the scrapper-tool REST sidecar.

Run after editing :mod:`scrapper_tool.http_server`::

    uv run python scripts/dump_openapi.py

Outputs:

- ``docs/openapi/openapi.json``  — full OpenAPI 3.1 JSON spec
- ``docs/openapi/openapi.yaml``  — same spec in YAML (codegen-friendly)

Both files are committed to the repo so external consumers (the
affiliate service, OpenAPI client codegen, doc generators) can read the
spec without running a container. The CI ``openapi-spec-check`` job
fails if these files drift from the in-code spec — re-run this script
to fix the drift.

Codegen example::

    uv run openapi-python-client generate --path docs/openapi/openapi.yaml
    npx openapi-typescript-codegen --input docs/openapi/openapi.yaml \\
        --output ./src/scrapper-client
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from scrapper_tool.http_server import _build_app

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs" / "openapi"


def main() -> int:
    """Generate openapi.json + openapi.yaml under docs/openapi/."""
    app = _build_app(api_key=None, cors_origins=["*"], serve_docs=True)
    spec = app.openapi()

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = DOCS_DIR / "openapi.json"
    yaml_path = DOCS_DIR / "openapi.yaml"

    json_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    yaml_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    print(f"Wrote {json_path}")
    print(f"Wrote {yaml_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
