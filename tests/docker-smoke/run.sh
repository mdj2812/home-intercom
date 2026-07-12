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
cat > "${TMPDIR}/rooms.json" << 'EOF'
{
  "test": {
    "name": "Test Room",
    "entity_id": "media_player.test_speaker"
  }
}
EOF

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

# 1. /version
VER=$(curl -sS "${URL}/version" 2>/dev/null || echo "")
if echo "${VER}" | grep -q '"version"'; then
    echo "  ✅ GET /version — ${VER}"
else
    echo "  ❌ GET /version — unexpected: ${VER}"
    exit 1
fi

# 2. /rooms
ROOMS=$(curl -sS "${URL}/rooms" 2>/dev/null || echo "")
if echo "${ROOMS}" | grep -q '"test"'; then
    echo "  ✅ GET /rooms — test room found"
else
    echo "  ❌ GET /rooms — test room missing"
    echo "     Response: ${ROOMS}"
    exit 1
fi

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

echo "==> All Docker smoke tests passed! 🎉"
