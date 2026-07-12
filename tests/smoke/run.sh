#!/usr/bin/env bash
# Smoke test: start HA container, verify Home Intercom integration loads.
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
CONTAINER_NAME="ha-home-intercom-smoke"
HA_PORT="8123"
HA_URL="http://localhost:${HA_PORT}"
MAX_WAIT=180
POLL_INTERVAL=3

cleanup() {
    echo "==> Tearing down container..."
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
}
trap cleanup EXIT

# ── Build image ──────────────────────────────────────────────
if [ "${SKIP_BUILD}" = true ]; then
    echo "==> Skipping build (--skip-build), using home-intercom-ha-test:ci"
    IMAGE="home-intercom-ha-test:ci"
else
    echo "==> Building HA smoke-test image..."
    docker build \
        -t home-intercom-ha-test \
        -f "${SCRIPT_DIR}/Dockerfile.ha-test" \
        "$(git rev-parse --show-toplevel)"
    IMAGE="home-intercom-ha-test"
fi

# ── Start container ─────────────────────────────────────────
echo "==> Starting HA container..."
docker run -d \
    --name "${CONTAINER_NAME}" \
    -p "${HA_PORT}:${HA_PORT}" \
    "${IMAGE}"

# ── Wait for HA to be ready ─────────────────────────────────
echo "==> Waiting for Home Assistant to start (max ${MAX_WAIT}s)..."
elapsed=0
while [ "${elapsed}" -lt "${MAX_WAIT}" ]; do
    if docker exec "${CONTAINER_NAME}" \
        curl -sf "${HA_URL}/api/onboarding" -o /dev/null 2>/dev/null; then
        echo "==> Home Assistant is ready after ${elapsed}s"
        break
    fi
    sleep "${POLL_INTERVAL}"
    elapsed=$((elapsed + POLL_INTERVAL))
done

if [ "${elapsed}" -ge "${MAX_WAIT}" ]; then
    echo "ERROR: HA did not start within ${MAX_WAIT}s"
    docker logs "${CONTAINER_NAME}" --tail 50
    exit 1
fi

# Give HA a few more seconds to finish loading
sleep 10

# ── Verify no home_intercom errors ──────────────────────────
echo "==> Checking for Home Intercom errors in logs..."
ERRORS=$(docker logs "${CONTAINER_NAME}" 2>&1 | grep -i "home_intercom" | grep -iE "error|traceback|exception" || true)
if [ -n "${ERRORS}" ]; then
    echo "  ❌ Home Intercom errors found:"
    echo "${ERRORS}"
    exit 1
fi
echo "  ✅ No errors — integration loaded cleanly"

# ── Verify setup log ────────────────────────────────────────
echo "==> Checking Home Intercom in setup logs..."
SETUP_LOGS=$(docker logs "${CONTAINER_NAME}" 2>&1 | grep -i "Home Intercom" || true)
if [ -z "${SETUP_LOGS}" ]; then
    echo "  ⚠️  Home Intercom not mentioned in logs (may not have loaded)"
else
    echo "  ✅ Home Intercom found in logs:"
    echo "${SETUP_LOGS}" | head -5
fi

# ── Verify HTTP views registered ────────────────────────────
echo "==> Checking API endpoints..."

# 1. /api/home_intercom/version — returns version + pcm_rate
if docker exec "${CONTAINER_NAME}" \
    curl -sf "http://localhost:${HA_PORT}/api/home_intercom/version" -o /dev/null 2>/dev/null; then
    echo "  ✅ GET /api/home_intercom/version"
else
    echo "  ❌ GET /api/home_intercom/version not responding"
    exit 1
fi

# 2. /api/home_intercom/rooms — returns room configuration (public, no auth)
ROOMS=$(docker exec "${CONTAINER_NAME}" \
    curl -sf "http://localhost:${HA_PORT}/api/home_intercom/rooms" 2>/dev/null)
if echo "${ROOMS}" | grep -q '"test"'; then
    echo "  ✅ GET /api/home_intercom/rooms — test room found"
else
    echo "  ❌ GET /api/home_intercom/rooms — test room missing"
    echo "     Response: ${ROOMS}"
    exit 1
fi

# 3. /api/home_intercom/rooms/status — returns speaker status
STATUS=$(docker exec "${CONTAINER_NAME}" \
    curl -sf "http://localhost:${HA_PORT}/api/home_intercom/rooms/status" 2>/dev/null || echo "{}")
if echo "${STATUS}" | grep -q '"test"'; then
    echo "  ✅ GET /api/home_intercom/rooms/status — test room in response"
else
    echo "  ⚠️  GET /api/home_intercom/rooms/status — no test room (may need real media_player)"
fi

# 4. /api/home_intercom/panel — returns PWA frontend HTML
PANEL=$(docker exec "${CONTAINER_NAME}" \
    curl -sf "http://localhost:${HA_PORT}/api/home_intercom/panel" 2>/dev/null || echo "")
if echo "${PANEL}" | grep -qi '<!DOCTYPE html>'; then
    echo "  ✅ GET /api/home_intercom/panel — HTML returned"
else
    echo "  ❌ GET /api/home_intercom/panel — not HTML"
    echo "     First 100 chars: ${PANEL:0:100}"
    exit 1
fi

echo "==> All smoke tests passed! 🎉"
