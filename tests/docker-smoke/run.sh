#!/usr/bin/env bash
# Smoke test: build + start Docker intercom server, verify it responds.
#
# Usage:
#   ./run.sh              Build image, start, verify
#   ./run.sh --skip-build  Skip build (CI already built with cache)
set -euo pipefail

SKIP_BUILD=false
if [[ "${1:-}" == "--skip-build" ]]; then
    SKIP_BUILD=true
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTAINER_NAME="home-intercom-docker-smoke"
PORT="8764"
URL="http://localhost:${PORT}"
MAX_WAIT=30
POLL_INTERVAL=2

cleanup() {
    echo "==> Tearing down container..."
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
}
trap cleanup EXIT

# ── Build image ──────────────────────────────────────────────
if [ "${SKIP_BUILD}" = true ]; then
    echo "==> Skipping build, using home-intercom:ci"
    IMAGE="home-intercom:ci"
else
    echo "==> Building Docker image..."
    docker build \
        -t home-intercom \
        -f docker/Dockerfile \
        "$(git rev-parse --show-toplevel)"
    IMAGE="home-intercom"
fi

# ── Create minimal rooms.json for testing ────────────────────
TMPDIR=$(mktemp -d)
EXPECTED_ROOMS='{"test":{"name":"Test Room","entity_id":"media_player.test_speaker"}}'
echo "${EXPECTED_ROOMS}" > "${TMPDIR}/rooms.json"

# ── Start container ─────────────────────────────────────────
echo "==> Starting intercom container..."
docker run -d \
    --name "${CONTAINER_NAME}" \
    -v "${TMPDIR}/rooms.json:/app/rooms.json:ro" \
    -p "${PORT}:${PORT}" \
    -e HA_URL="http://ha:8123" \
    -e HA_TOKEN="fake-token" \
    "${IMAGE}"

# ── Wait for server to be ready ──────────────────────────────
echo "==> Waiting for server to start (max ${MAX_WAIT}s)..."
elapsed=0
while [ "${elapsed}" -lt "${MAX_WAIT}" ]; do
    if curl -sS "${URL}/version" -o /dev/null 2>/dev/null; then
        echo "==> Server ready after ${elapsed}s"
        break
    fi
    sleep "${POLL_INTERVAL}"
    elapsed=$((elapsed + POLL_INTERVAL))
done

if [ "${elapsed}" -ge "${MAX_WAIT}" ]; then
    echo "ERROR: Server did not start within ${MAX_WAIT}s"
    docker logs "${CONTAINER_NAME}" --tail 30
    exit 1
fi

# ── Verify endpoints ────────────────────────────────────────
echo "==> Checking endpoints..."

# 1. /version — verify exact fields
VER=$(curl -sS "${URL}/version" 2>/dev/null || echo "")
echo "${VER}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'version' in d and d['version'], 'missing version'
assert d.get('pcm_rate') == 16000, f'bad pcm_rate: {d.get(\"pcm_rate\")}'
print(f'ok: version={d[\"version\"]} pcm_rate={d[\"pcm_rate\"]}')
" 2>&1 && echo "  ✅ GET /version — ${VER}" || { echo "  ❌ GET /version — check failed"; exit 1; }

# 2. /rooms — verify matches input rooms.json
ROOMS=$(curl -sS "${URL}/rooms" 2>/dev/null || echo "")
echo "${ROOMS}" | python3 -c "
import sys, json
got = json.load(sys.stdin)
expected = json.loads('${EXPECTED_ROOMS}')
assert got == expected, f'mismatch\\n  got:      {json.dumps(got)}\\n  expected: {json.dumps(expected)}'
print('ok: rooms match input')
" 2>&1 && echo "  ✅ GET /rooms — matches input" || { echo "  ❌ GET /rooms — output mismatch"; exit 1; }

# 3. / — PWA frontend
INDEX=$(curl -sS "${URL}/" 2>/dev/null || echo "")
if echo "${INDEX}" | grep -q '<'; then
    echo "  ✅ GET / — HTML returned"
elif [ -n "${INDEX}" ]; then
    echo "  ⚠️  GET / — responded but not HTML"
    echo "     First 100 chars: ${INDEX:0:100}"
else
    echo "  ❌ GET / — empty response"
    exit 1
fi

# 4. /static/icon-192.png
ICON_CODE=$(curl -sS -o /dev/null -w '%{http_code}' "${URL}/static/icon-192.png" 2>/dev/null || echo "000")
if [ "${ICON_CODE}" = "200" ]; then
    echo "  ✅ GET /static/icon-192.png — HTTP 200"
else
    echo "  ❌ GET /static/icon-192.png — HTTP ${ICON_CODE}"
    exit 1
fi

# 5. HA-compatible /api/home_intercom/version — exact match
VER_HA=$(curl -sS "${URL}/api/home_intercom/version" 2>/dev/null || echo "")
if [ "${VER_HA}" = "${VER}" ]; then
    echo "  ✅ GET /api/home_intercom/version — matches /version"
else
    echo "  ❌ GET /api/home_intercom/version — differs from /version"
    echo "     /version:                ${VER}"
    echo "     /api/home_intercom/version: ${VER_HA}"
    exit 1
fi

# 6. HA-compatible /api/home_intercom/rooms — exact match
ROOMS_HA=$(curl -sS "${URL}/api/home_intercom/rooms" 2>/dev/null || echo "")
if [ "${ROOMS_HA}" = "${ROOMS}" ]; then
    echo "  ✅ GET /api/home_intercom/rooms — matches /rooms"
else
    echo "  ❌ GET /api/home_intercom/rooms — differs from /rooms"
    exit 1
fi

# 7. HA-compatible /api/home_intercom/static/icon-192.png
ICON_HA_CODE=$(curl -sS -o /dev/null -w '%{http_code}' "${URL}/api/home_intercom/static/icon-192.png" 2>/dev/null || echo "000")
if [ "${ICON_HA_CODE}" = "200" ]; then
    echo "  ✅ GET /api/home_intercom/static/icon-192.png — HTTP 200"
else
    echo "  ❌ GET /api/home_intercom/static/icon-192.png — HTTP ${ICON_HA_CODE}"
    exit 1
fi

echo "==> All Docker smoke tests passed! 🎉"
