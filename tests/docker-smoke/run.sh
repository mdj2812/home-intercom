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

# Serialize concurrent local runs — a second invocation would collide on the
# fixed container name/port and its teardown would kill this run's container.
LOCK_FILE="/tmp/home-intercom-docker-smoke.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "❌ Another docker-smoke run is in progress (lock: ${LOCK_FILE})"
    exit 1
fi

cleanup() {
    echo "==> Tearing down container..."
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
}
trap cleanup EXIT

# Remove leftovers from previously interrupted runs before starting
docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true

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
# Note: Docker room entries use "entity" (not HA's "entity_id") — /record reads it.
TMPDIR=$(mktemp -d)
EXPECTED_ROOMS='{"test":{"name":"Test Room","entity":"media_player.test_speaker"}}'
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

# 8. POST /api/home_intercom/devices/hello — ESP32 registration (issue #37)
HELLO=$(curl -sS -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" -H "Content-Type: application/json" \
    -d '{"firmware_version": "smoke-1.0"}' "${URL}/api/home_intercom/devices/hello" 2>/dev/null || echo "")
echo "${HELLO}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d.get('status') == 'ok', f'bad status: {d}'
assert d.get('sample_rate') == 16000, f'bad sample_rate: {d}'
assert d.get('max_record_secs') == 60, f'bad max_record_secs: {d}'
assert 'device_name' in d and 'room' in d, f'missing fields: {d}'
print(f'ok: hello={d}')
" 2>&1 && echo "  ✅ POST /api/home_intercom/devices/hello — ${HELLO}" || { echo "  ❌ POST /api/home_intercom/devices/hello — check failed"; exit 1; }

# 9. POST /api/home_intercom/devices/hello — invalid MAC rejected
HELLO_BAD=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "X-Device-ID: not-a-mac" \
    "${URL}/api/home_intercom/devices/hello" 2>/dev/null || echo "000")
if [ "${HELLO_BAD}" = "400" ]; then
    echo "  ✅ POST /api/home_intercom/devices/hello — invalid MAC → 400"
else
    echo "  ❌ POST /api/home_intercom/devices/hello — invalid MAC gave HTTP ${HELLO_BAD}, want 400"
    exit 1
fi

# 10. POST /record with registered MAC → allowed (issue #47)
python3 -c "
import struct, sys
hdr = b'RIFF' + struct.pack('<I', 36+64) + b'WAVEfmt ' + struct.pack('<I',16) + (1).to_bytes(2,'little') + (1).to_bytes(2,'little') + (16000).to_bytes(4,'little') + (32000).to_bytes(4,'little') + (2).to_bytes(2,'little') + (16).to_bytes(2,'little') + b'data' + struct.pack('<I', 64)
sys.stdout.buffer.write(hdr + b'\x00' * 64)
" > "${TMPDIR}/test.wav"
REC_CODE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" \
    --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test" 2>/dev/null || echo "000")
if [ "${REC_CODE}" = "200" ]; then
    echo "  ✅ POST /record — registered MAC → 200"
else
    echo "  ❌ POST /record — registered MAC gave HTTP ${REC_CODE}, want 200"
    exit 1
fi

# 11. POST /record with unknown MAC → 403
REC_BAD=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "X-Device-ID: 11:22:33:44:55:66" \
    --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test" 2>/dev/null || echo "000")
if [ "${REC_BAD}" = "403" ]; then
    echo "  ✅ POST /record — unknown MAC → 403"
else
    echo "  ❌ POST /record — unknown MAC gave HTTP ${REC_BAD}, want 403"
    exit 1
fi

# 12. Device registry persisted to disk
if docker exec "${CONTAINER_NAME}" grep -q "AA:BB:CC:DD:EE:FF" /data/device_registry.json 2>/dev/null; then
    echo "  ✅ device registry persisted to /data/device_registry.json"
else
    echo "  ❌ /data/device_registry.json missing the registered MAC"
    docker exec "${CONTAINER_NAME}" cat /data/device_registry.json 2>&1 || true
    exit 1
fi

# 13. Registry survives a container restart — record without re-hello
docker restart "${CONTAINER_NAME}" >/dev/null
echo "==> Container restarted, waiting for server (persistence check)..."
ELAPSED=0
until curl -sS "${URL}/version" -o /dev/null 2>/dev/null; do
    sleep "${POLL_INTERVAL}"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
    if [ "${ELAPSED}" -ge "${MAX_WAIT}" ]; then
        echo "  ❌ Server did not come back after restart"
        exit 1
    fi
done
REC_RESTART=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" \
    --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test" 2>/dev/null || echo "000")
if [ "${REC_RESTART}" = "200" ]; then
    echo "  ✅ POST /record after restart (no re-hello) — registry reloaded from disk → 200"
else
    echo "  ❌ POST /record after restart gave HTTP ${REC_RESTART}, want 200 (registry not persisted?)"
    echo "  --- registry file after restart:"
    docker exec "${CONTAINER_NAME}" cat /data/device_registry.json 2>&1 || true
    echo "  --- container logs (tail):"
    docker logs "${CONTAINER_NAME}" --tail 20 2>&1 || true
    exit 1
fi

echo "==> All Docker smoke tests passed! 🎉"
