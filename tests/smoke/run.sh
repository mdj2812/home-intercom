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
MAX_WAIT=300
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

# Give HA extra time to finish integration setup
sleep 15

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

# ── Verify API endpoints ────────────────────────────────────
echo "==> Checking API endpoints..."

# 1. /api/home_intercom/version
VER=$(docker exec "${CONTAINER_NAME}" \
    curl -sS "http://localhost:${HA_PORT}/api/home_intercom/version" 2>/dev/null || echo "")
if echo "${VER}" | grep -q '"version"'; then
    echo "  ✅ GET /api/home_intercom/version — ${VER}"
else
    echo "  ❌ GET /api/home_intercom/version — unexpected: ${VER}"
    exit 1
fi

# 1b. /api/home_intercom/config — global audio settings (issue #39)
CFG=$(docker exec "${CONTAINER_NAME}" \
    curl -sS "http://localhost:${HA_PORT}/api/home_intercom/config" 2>/dev/null || echo "")
echo "${CFG}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d.get('sample_rate') == 16000, f'bad sample_rate: {d}'
assert d.get('max_record_secs') == 60, f'bad max_record_secs: {d}'
" 2>&1 && echo "  ✅ GET /api/home_intercom/config — ${CFG}" || {
    echo "  ❌ GET /api/home_intercom/config — unexpected: ${CFG}"
    exit 1
}

# 2. /api/home_intercom/rooms
ROOMS=$(docker exec "${CONTAINER_NAME}" \
    curl -sS "http://localhost:${HA_PORT}/api/home_intercom/rooms" 2>/dev/null || echo "")
if echo "${ROOMS}" | grep -q '"test"'; then
    echo "  ✅ GET /api/home_intercom/rooms — test room found"
else
    echo "  ❌ GET /api/home_intercom/rooms — test room missing"
    echo "     Response: ${ROOMS}"
    exit 1
fi

# 3. /api/home_intercom/rooms/status
STATUS=$(docker exec "${CONTAINER_NAME}" \
    curl -sS "http://localhost:${HA_PORT}/api/home_intercom/rooms/status" 2>/dev/null || echo "{}")
if echo "${STATUS}" | grep -q '"test"'; then
    echo "  ✅ GET /api/home_intercom/rooms/status — test room in response"
else
    echo "  ⚠️  GET /api/home_intercom/rooms/status — no test room (may need real media_player)"
fi

# 4. /home_intercom — PWA frontend HTML
PANEL_CODE=$(docker exec "${CONTAINER_NAME}" \
    curl -sS -o /dev/null -w '%{http_code}' "http://localhost:${HA_PORT}/home_intercom" 2>/dev/null || echo "000")
PANEL=$(docker exec "${CONTAINER_NAME}" \
    curl -sSL "http://localhost:${HA_PORT}/home_intercom" 2>/dev/null || echo "")
if echo "${PANEL}" | grep -q '<'; then
    echo "  ✅ GET /home_intercom — HTML returned (HTTP ${PANEL_CODE})"
elif [ -n "${PANEL}" ]; then
    echo "  ⚠️  GET /home_intercom — responded but not HTML (HTTP ${PANEL_CODE})"
    echo "     First 100 chars: ${PANEL:0:100}"
else
    echo "  ❌ GET /home_intercom — empty response (HTTP ${PANEL_CODE})"
    exit 1
fi

# 5. POST /api/home_intercom/devices/hello — ESP32 registration (issue #37)
HELLO=$(docker exec "${CONTAINER_NAME}" \
    curl -sS -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" -H "Content-Type: application/json" \
    -d '{"firmware_version": "smoke-1.0"}' \
    "http://localhost:${HA_PORT}/api/home_intercom/devices/hello" 2>/dev/null || echo "")
if echo "${HELLO}" | grep -q '"status": *"ok"'; then
    echo "  ✅ POST /api/home_intercom/devices/hello — ${HELLO}"
else
    echo "  ❌ POST /api/home_intercom/devices/hello — unexpected: ${HELLO}"
    exit 1
fi

# 6. POST /api/home_intercom/devices/hello — invalid MAC rejected
HELLO_BAD=$(docker exec "${CONTAINER_NAME}" \
    curl -sS -o /dev/null -w '%{http_code}' -X POST -H "X-Device-ID: not-a-mac" \
    "http://localhost:${HA_PORT}/api/home_intercom/devices/hello" 2>/dev/null || echo "000")
if [ "${HELLO_BAD}" = "400" ]; then
    echo "  ✅ POST /api/home_intercom/devices/hello — invalid MAC → 400"
else
    echo "  ❌ POST /api/home_intercom/devices/hello — invalid MAC gave HTTP ${HELLO_BAD}, want 400"
    exit 1
fi

# 6b. GET /api/home_intercom/devices — PWA-token-gated read-only listing (issue #52)
PWA_TOKEN=$(docker exec "${CONTAINER_NAME}" \
    python3 -c "import json; print(json.load(open('/config/.storage/home_intercom.pwa_token'))['data']['token'])" 2>/dev/null || echo "")
DEV_NOAUTH=$(docker exec "${CONTAINER_NAME}" \
    curl -sS -o /dev/null -w '%{http_code}' \
    "http://localhost:${HA_PORT}/api/home_intercom/devices" 2>/dev/null || echo "000")
DEVICES=$(docker exec "${CONTAINER_NAME}" \
    curl -sS -H "X-PWA-Token: ${PWA_TOKEN}" \
    "http://localhost:${HA_PORT}/api/home_intercom/devices" 2>/dev/null || echo "")
if [ "${DEV_NOAUTH}" = "401" ] && echo "${DEVICES}" | grep -q "AA:BB:CC:DD:EE:FF"; then
    echo "  ✅ GET /api/home_intercom/devices — no token → 401, valid token lists registered MAC"
else
    echo "  ❌ GET /api/home_intercom/devices — noauth=${DEV_NOAUTH} (want 401), with token: ${DEVICES}"
    exit 1
fi

# 7. POST /api/home_intercom/device/record with registered MAC → allowed (issue #47)
docker exec "${CONTAINER_NAME}" python3 -c "
import struct, sys
hdr = b'RIFF' + struct.pack('<I', 36+64) + b'WAVEfmt ' + struct.pack('<I',16) + (1).to_bytes(2,'little') + (1).to_bytes(2,'little') + (16000).to_bytes(4,'little') + (32000).to_bytes(4,'little') + (2).to_bytes(2,'little') + (16).to_bytes(2,'little') + b'data' + struct.pack('<I', 64)
open('/tmp/test.wav','wb').write(hdr + b'\x00' * 64)
"
REC_CODE=$(docker exec "${CONTAINER_NAME}" \
    curl -sS -o /dev/null -w '%{http_code}' -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" \
    --data-binary @/tmp/test.wav \
    "http://localhost:${HA_PORT}/api/home_intercom/device/record?target=test" 2>/dev/null || echo "000")
if [ "${REC_CODE}" = "200" ]; then
    echo "  ✅ POST /api/home_intercom/device/record — registered MAC → 200"
else
    echo "  ❌ POST /api/home_intercom/device/record — registered MAC gave HTTP ${REC_CODE}, want 200"
    exit 1
fi

# 8. POST /api/home_intercom/device/record with unknown MAC → 403
REC_BAD=$(docker exec "${CONTAINER_NAME}" \
    curl -sS -o /dev/null -w '%{http_code}' -X POST -H "X-Device-ID: 11:22:33:44:55:66" \
    --data-binary @/tmp/test.wav \
    "http://localhost:${HA_PORT}/api/home_intercom/device/record?target=test" 2>/dev/null || echo "000")
if [ "${REC_BAD}" = "403" ]; then
    echo "  ✅ POST /api/home_intercom/device/record — unknown MAC → 403"
else
    echo "  ❌ POST /api/home_intercom/device/record — unknown MAC gave HTTP ${REC_BAD}, want 403"
    exit 1
fi

echo "==> All smoke tests passed! 🎉"