#!/usr/bin/env bash
# Docker image smoke (Phase 7.5 follow-up — v1.1.2).
#
# Boots a freshly-built scrapper-tool image and asserts:
#   1. /health returns 200 within 60s of container start.
#   2. /version reports the expected build version.
#   3. /ready reports status="ready" AND checks.agent_runnable=true.
#      (This is the load-bearing gate — pre-1.1.2 the published image
#      lied: agent_installed=true while Firefox was missing on disk.)
#   4. POST /scrape mode=fetch against https://example.com returns
#      a 200 with a populated raw_text. This proves Pattern A/B/C is
#      reachable end-to-end in the image without hitting the network
#      for an LLM (we cannot assume Ollama / LM Studio in CI).
#
# Usage:
#   IMAGE=scrapper-tool:test bash tests/integration/test_image_smoke.sh
#   IMAGE=scrapper-tool:test EXPECT_VERSION=1.1.2 bash tests/integration/test_image_smoke.sh
#
# Exit codes:
#   0 — green
#   1 — image won't boot (no /health within 60s)
#   2 — /ready not 'ready' or agent_runnable missing/false
#   3 — /version mismatch
#   4 — /scrape mode=fetch failed
#
# CI invokes this from .github/workflows/docker-release.yml AFTER the
# image is built but BEFORE the tag is pushed. A red smoke aborts
# the release.

set -euo pipefail

IMAGE="${IMAGE:-scrapper-tool:test}"
EXPECT_VERSION="${EXPECT_VERSION:-}"  # empty = don't pin
PORT="${PORT:-15792}"  # high port to avoid colliding with a host sidecar
API_KEY="${API_KEY:-smoke-test-key}"
CONTAINER_NAME="${CONTAINER_NAME:-scrapper-tool-smoke-$$}"
WAIT_SECONDS="${WAIT_SECONDS:-60}"

require() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 99; } ; }
require docker
require curl
require jq

cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[smoke] starting ${IMAGE} as ${CONTAINER_NAME} on port ${PORT}"
docker run -d --name "${CONTAINER_NAME}" \
    -p "${PORT}:5792" \
    -e "SCRAPPER_TOOL_HTTP_API_KEY=${API_KEY}" \
    "${IMAGE}" >/dev/null

echo "[smoke] 1/4 waiting up to ${WAIT_SECONDS}s for /health ..."
ok=0
for _ in $(seq 1 "${WAIT_SECONDS}"); do
    if curl -fsS -m 2 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
        ok=1
        break
    fi
    sleep 1
done
if [[ "${ok}" -ne 1 ]]; then
    echo "[smoke] FAIL — /health did not respond within ${WAIT_SECONDS}s" >&2
    docker logs "${CONTAINER_NAME}" >&2 || true
    exit 1
fi
echo "[smoke] 1/4 OK — /health up"

echo "[smoke] 2/4 probing /version ..."
version_body=$(curl -fsS -m 5 "http://localhost:${PORT}/version")
echo "  ${version_body}"
if [[ -n "${EXPECT_VERSION}" ]]; then
    actual=$(echo "${version_body}" | jq -r '.version')
    if [[ "${actual}" != "${EXPECT_VERSION}" ]]; then
        echo "[smoke] FAIL — /version is '${actual}', expected '${EXPECT_VERSION}'" >&2
        exit 3
    fi
fi
echo "[smoke] 2/4 OK — /version"

echo "[smoke] 3/4 probing /ready (gate: status=ready AND agent_runnable=true) ..."
ready_body=$(curl -fsS -m 10 "http://localhost:${PORT}/ready")
echo "  ${ready_body}"
status=$(echo "${ready_body}" | jq -r '.status')
agent_runnable=$(echo "${ready_body}" | jq -r '.checks.agent_runnable')

# /ready can legitimately be 'degraded' in CI when no LLM is reachable —
# we only fail-loud on agent_runnable being false. agent_runnable=true
# is the load-bearing assertion: it proves the image's Firefox binary
# is actually on disk (the v1.1.0 false-positive that drove this WP).
if [[ "${agent_runnable}" != "true" ]]; then
    echo "[smoke] FAIL — agent_runnable=${agent_runnable} (expected true)." >&2
    echo "[smoke]        Pre-v1.1.2 the image declared agent_installed=true" >&2
    echo "[smoke]        while Firefox was missing — this assertion catches that." >&2
    exit 2
fi
case "${status}" in
    ready|degraded)
        echo "[smoke] 3/4 OK — /ready status=${status}, agent_runnable=true"
        ;;
    *)
        echo "[smoke] FAIL — /ready status='${status}' (expected ready|degraded)" >&2
        exit 2
        ;;
esac

echo "[smoke] 4/4 POST /scrape mode=fetch against https://example.com ..."
scrape_body=$(curl -fsS -m 30 \
    -H "X-API-Key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -X POST "http://localhost:${PORT}/scrape" \
    -d '{"url":"https://example.com/","mode":"fetch"}')
pattern=$(echo "${scrape_body}" | jq -r '.pattern_used')
raw_len=$(echo "${scrape_body}" | jq -r '.raw_text | length // 0')
if [[ "${pattern}" != "a_b_c" ]]; then
    echo "[smoke] FAIL — /scrape pattern_used='${pattern}' (expected a_b_c)" >&2
    echo "${scrape_body}" >&2
    exit 4
fi
if [[ "${raw_len}" -lt 100 ]]; then
    echo "[smoke] FAIL — /scrape returned raw_text len=${raw_len} (expected >=100)" >&2
    exit 4
fi
echo "[smoke] 4/4 OK — pattern=${pattern}, raw_text len=${raw_len}"

echo "[smoke] all checks passed"
