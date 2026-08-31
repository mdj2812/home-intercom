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

# ── Helpers ──────────────────────────────────────────────────
fetch() {
    curl -sS "$@" 2>/dev/null || echo ""
}

fetch_code() {
    curl -sS -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || echo "000"
}

assert_json() {
    local label="$1"
    local body="$2"
    local script="$3"
    local detail
    detail=$(echo "${body}" | python3 -c "${script}" 2>&1) || {
        echo "  ❌ ${label} — check failed"
        echo "${detail}"
        exit 1
    }
    if [ -n "${detail}" ]; then
        echo "  ✅ ${label} — ${detail#ok: }"
    else
        echo "  ✅ ${label}"
    fi
}

assert_http() {
    local label="$1"
    local got="$2"
    local want="$3"
    if [ "${got}" = "${want}" ]; then
        echo "  ✅ ${label} — HTTP ${want}"
    else
        echo "  ❌ ${label} — HTTP ${got}, want ${want}"
        exit 1
    fi
}

assert_eq() {
    local label="$1"
    local a="$2"
    local b="$3"
    if [ "${a}" = "${b}" ]; then
        echo "  ✅ ${label}"
    else
        echo "  ❌ ${label}"
        [ -n "${a}" ] && echo "     left:  ${a}"
        [ -n "${b}" ] && echo "     right: ${b}"
        exit 1
    fi
}

make_test_wav() {
    local path="$1"
    python3 -c "
import struct
hdr = b'RIFF' + struct.pack('<I', 36+64) + b'WAVEfmt ' + struct.pack('<I',16) + (1).to_bytes(2,'little') + (1).to_bytes(2,'little') + (16000).to_bytes(4,'little') + (32000).to_bytes(4,'little') + (2).to_bytes(2,'little') + (16).to_bytes(2,'little') + b'data' + struct.pack('<I', 64)
open('${path}', 'wb').write(hdr + b'\x00' * 64)
"
}

wait_for_server() {
    local elapsed=0
    while [ "${elapsed}" -lt "${MAX_WAIT}" ]; do
        if fetch "${URL}/version" -o /dev/null; then
            echo "==> Server ready after ${elapsed}s"
            return 0
        fi
        sleep "${POLL_INTERVAL}"
        elapsed=$((elapsed + POLL_INTERVAL))
    done
    return 1
}

assert_ha_alias() {
    local label="$1"
    local primary="$2"
    local alias_path="$3"
    assert_eq "${label}" "$(fetch "${URL}${alias_path}")" "${primary}"
}

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
if ! wait_for_server; then
    echo "ERROR: Server did not start within ${MAX_WAIT}s"
    docker logs "${CONTAINER_NAME}" --tail 30
    exit 1
fi

# ── Verify endpoints ────────────────────────────────────────
echo "==> Checking endpoints..."

make_test_wav "${TMPDIR}/test.wav"

# 1. /version — verify version field
VER=$(fetch "${URL}/version")
assert_json "GET /version" "${VER}" "
import sys, json
d = json.load(sys.stdin)
assert 'version' in d and d['version'], 'missing version'
print(f'ok: version={d[\"version\"]}')
"

# 1b. /config — global audio settings (issue #39)
CFG=$(fetch "${URL}/config")
assert_json "GET /config" "${CFG}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('sample_rate') == 16000, f'bad sample_rate: {d}'
assert d.get('max_record_secs') == 60, f'bad max_record_secs: {d}'
print(f'ok: config={d}')
"
assert_ha_alias "GET /api/home_intercom/config — matches /config" "${CFG}" "/api/home_intercom/config"

# 2. /rooms — verify matches input rooms.json
ROOMS=$(fetch "${URL}/rooms")
assert_json "GET /rooms — matches input" "${ROOMS}" "
import sys, json
got = json.load(sys.stdin)
expected = json.loads('${EXPECTED_ROOMS}')
assert got == expected, f'mismatch\\n  got:      {json.dumps(got)}\\n  expected: {json.dumps(expected)}'
print('ok: rooms match input')
"

# 3. / — PWA frontend
INDEX=$(fetch "${URL}/")
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
assert_http "GET /static/icon-192.png" "$(fetch_code "${URL}/static/icon-192.png")" "200"

# 5–7. HA-compatible aliases
assert_ha_alias "GET /api/home_intercom/version — matches /version" "${VER}" "/api/home_intercom/version"
assert_ha_alias "GET /api/home_intercom/rooms — matches /rooms" "${ROOMS}" "/api/home_intercom/rooms"
assert_http "GET /api/home_intercom/static/icon-192.png" \
    "$(fetch_code "${URL}/api/home_intercom/static/icon-192.png")" "200"

# 8. POST /api/home_intercom/devices/hello — ESP32 registration (issue #37)
HELLO=$(fetch -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" -H "Content-Type: application/json" \
    -d '{"firmware_version": "smoke-1.0"}' "${URL}/api/home_intercom/devices/hello")
assert_json "POST /api/home_intercom/devices/hello" "${HELLO}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('status') == 'ok', f'bad status: {d}'
assert d.get('sample_rate') == 16000, f'bad sample_rate: {d}'
assert d.get('max_record_secs') == 60, f'bad max_record_secs: {d}'
assert 'device_name' in d and 'room' in d, f'missing fields: {d}'
print(f'ok: hello={d}')
"

# 9. POST /api/home_intercom/devices/hello — invalid MAC rejected
assert_http "POST /api/home_intercom/devices/hello — invalid MAC → 400" \
    "$(fetch_code -X POST -H "X-Device-ID: not-a-mac" "${URL}/api/home_intercom/devices/hello")" "400"

# 10–11. POST /record — MAC allow/deny (issue #47)
assert_http "POST /record — registered MAC → 200" \
    "$(fetch_code -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" \
        --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test")" "200"
assert_http "POST /record — unknown MAC → 403" \
    "$(fetch_code -X POST -H "X-Device-ID: 11:22:33:44:55:66" \
        --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test")" "403"

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
if ! wait_for_server; then
    echo "  ❌ Server did not come back after restart"
    exit 1
fi
assert_http "POST /record after restart (no re-hello) — registry reloaded from disk → 200" \
    "$(fetch_code -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" \
        --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test")" "200"

# 14–16. /chime — custom pre-announce (issue #66)
CHIME=$(fetch "${URL}/chime")
assert_json "GET /chime — default" "${CHIME}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('custom') is False, f'expected default chime: {d}'
assert 'url' in d and 'default_url' in d, f'missing fields: {d}'
print(f'ok: chime={d}')
"

CHIME_POST=$(fetch -X POST --data-binary @"${TMPDIR}/test.wav" "${URL}/chime")
assert_json "POST /chime — custom uploaded" "${CHIME_POST}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True, f'upload failed: {d}'
assert d.get('custom') is True, f'missing custom flag: {d}'
assert 'url' in d and 'custom_chime.wav' in d['url'], f'bad url: {d}'
print(f'ok: upload={d}')
"

CHIME_CUSTOM=$(fetch "${URL}/chime")
assert_json "GET /chime — custom active" "${CHIME_CUSTOM}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('custom') is True, f'expected custom chime: {d}'
print('ok: custom active')
"

assert_http "GET /audio/custom_chime.wav" \
    "$(fetch_code "${URL}/audio/custom_chime.wav")" "200"

CHIME_DEL=$(fetch -X DELETE "${URL}/chime")
assert_json "DELETE /chime — reset to default" "${CHIME_DEL}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True and d.get('custom') is False, f'bad delete response: {d}'
print('ok: reset to default')
"

assert_ha_alias "GET /api/home_intercom/chime — matches /chime (default)" \
    "${CHIME}" "/api/home_intercom/chime"

echo "==> All Docker smoke tests passed! 🎉"
