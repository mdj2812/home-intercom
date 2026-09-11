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
# Plain curl wrapper — preserves exit code (do not append || echo here).
fetch() {
    curl -sS "$@" 2>/dev/null
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
    assert_eq "${label}" "$(fetch "${URL}${alias_path}" || echo "")" "${primary}"
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

# ── Runtime data volume (issue #75) ──────────────────────────
# Live catalog is /data/rooms.json. A fresh volume starts empty; rooms
# are added via PUT /rooms/<id> (PWA). Docker entries use "entity".
TMPDIR=$(mktemp -d)
EXPECTED_ROOMS='{"test":{"name":"Test Room","entity":"media_player.test_speaker"}}'
mkdir -p "${TMPDIR}/data"

# ── Start container ─────────────────────────────────────────
echo "==> Starting intercom container..."
docker run -d \
    --name "${CONTAINER_NAME}" \
    -v "${TMPDIR}/data:/data" \
    -p "${PORT}:${PORT}" \
    -e HA_URL="http://ha:8123" \
    -e HA_TOKEN="fake-token" \
    -e HOME_INTERCOM_PENDING_HELLO_WAIT="0" \
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
VER=$(fetch "${URL}/version" || echo "")
assert_json "GET /version" "${VER}" "
import sys, json
d = json.load(sys.stdin)
assert 'version' in d and d['version'], 'missing version'
print(f'ok: version={d[\"version\"]}')
"

# 1c. GET /api/home_intercom/firmware — empty cache is 404
assert_http "GET /api/home_intercom/firmware — empty cache → 404" \
    "$(fetch_code "${URL}/api/home_intercom/firmware")" "404"

# 1b. /config — global audio settings (issue #39)
CFG=$(fetch "${URL}/config" || echo "")
assert_json "GET /config" "${CFG}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('sample_rate') == 16000, f'bad sample_rate: {d}'
assert d.get('max_record_secs') == 60, f'bad max_record_secs: {d}'
print(f'ok: config={d}')
"
assert_ha_alias "GET /api/home_intercom/config — matches /config" "${CFG}" "/api/home_intercom/config"

# 1d. GET /media_players — catalog is a JSON array (empty when HA is unreachable)
PLAYERS=$(fetch "${URL}/media_players" || echo "")
assert_json "GET /media_players — JSON array" "${PLAYERS}" "
import sys, json
d = json.load(sys.stdin)
assert isinstance(d, list), f'expected list, got: {d}'
print(f'ok: n={len(d)}')
"
assert_ha_alias "GET /api/home_intercom/media_players — matches /media_players" "${PLAYERS}" "/api/home_intercom/media_players"

# 2. /rooms — empty until the PWA (or PUT) writes /data/rooms.json
ROOMS=$(fetch "${URL}/rooms" || echo "")
assert_json "GET /rooms — empty on first start" "${ROOMS}" "
import sys, json
got = json.load(sys.stdin)
assert got == {}, f'expected empty catalog, got: {json.dumps(got)}'
print('ok: empty catalog')
"
assert_http "GET /rooms.json — leftover alias removed" \
    "$(fetch_code "${URL}/rooms.json")" "404"
PUT_TEST=$(fetch -X PUT -H "Content-Type: application/json" \
    -d '{"name":"Test Room","entity":"media_player.test_speaker"}' \
    "${URL}/rooms/test" || echo "")
assert_json "PUT /rooms/test" "${PUT_TEST}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True, f'not ok: {d}'
assert d['rooms']['test']['entity'] == 'media_player.test_speaker'
print('ok: test room created')
"

# 2b. PUT/PATCH/DELETE /rooms/<id> — writable catalog (issue #72)
PUT=$(fetch -X PUT -H "Content-Type: application/json" \
    -d '{"name":"Office","entity":"media_player.office","announce_volume":40}' \
    "${URL}/rooms/office" || echo "")
assert_json "PUT /rooms/office" "${PUT}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True, f'not ok: {d}'
assert d['rooms']['office']['entity'] == 'media_player.office'
print('ok: office created')
"
PATCH=$(fetch -X PATCH -H "Content-Type: application/json" \
    -d '{"name":"Study"}' "${URL}/rooms/office" || echo "")
assert_json "PATCH /rooms/office" "${PATCH}" "
import sys, json
d = json.load(sys.stdin)
assert d['rooms']['office']['name'] == 'Study', d
print('ok: renamed')
"
assert_ha_alias "PUT /api/home_intercom/rooms/office — matches /rooms after write" \
    "$(fetch "${URL}/rooms" || echo "")" "/api/home_intercom/rooms"
ORDER=$(fetch -X PUT -H "Content-Type: application/json" \
    -d '{"order":["office","test"]}' "${URL}/rooms/order" || echo "")
assert_json "PUT /rooms/order" "${ORDER}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True, f'not ok: {d}'
assert list(d['rooms']) == ['office', 'test'], d
print('ok: reordered')
"
assert_eq "GET /rooms key order" \
    "$(fetch "${URL}/rooms" | python3 -c 'import sys,json; print(list(json.load(sys.stdin)))')" \
    "['office', 'test']"
DEL=$(fetch -X DELETE "${URL}/rooms/office" || echo "")
assert_json "DELETE /rooms/office" "${DEL}" "
import sys, json
d = json.load(sys.stdin)
assert 'office' not in d.get('rooms', {}), d
print('ok: office removed')
"
ROOMS=$(fetch "${URL}/rooms" || echo "")
assert_json "GET /rooms — test room remains after delete" "${ROOMS}" "
import sys, json
got = json.load(sys.stdin)
expected = json.loads('${EXPECTED_ROOMS}')
assert got == expected, f'mismatch\\n  got:      {json.dumps(got)}\\n  expected: {json.dumps(expected)}'
print('ok: test room remains')
"

# 3. / — PWA frontend
INDEX=$(fetch "${URL}/" || echo "")
if [[ "${INDEX}" == *'<'* ]]; then
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

# 8. POST /api/home_intercom/devices/hello — ESP32 registration (issue #37, #51)
HELLO=$(fetch -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" -H "Content-Type: application/json" \
    -d '{"firmware_version": "smoke-1.0", "pins": [13, 4, 5, 12]}' "${URL}/api/home_intercom/devices/hello" || echo "")
assert_json "POST /api/home_intercom/devices/hello — pending until approve" "${HELLO}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('status') == 'pending', f'expected pending, got: {d}'
assert 'device_name' not in d, f'pending hello must not deliver config: {d}'
print(f'ok: hello={d}')
"

# 9. POST /api/home_intercom/devices/hello — invalid MAC rejected
assert_http "POST /api/home_intercom/devices/hello — invalid MAC → 400" \
    "$(fetch_code -X POST -H "X-Device-ID: not-a-mac" "${URL}/api/home_intercom/devices/hello")" "400"

# 9b. GET /devices — read-only registry listing includes the hello-registered MAC (issue #52)
DEVICES=$(fetch "${URL}/devices" || echo "")
assert_json "GET /devices — registered MAC listed" "${DEVICES}" "
import sys, json
d = json.load(sys.stdin)
dev = d.get('AA:BB:CC:DD:EE:FF')
assert dev, f'registered MAC missing: {d}'
assert dev.get('name') == 'Device EE:FF', f'bad name: {dev}'
assert dev.get('firmware_version') == 'smoke-1.0', f'bad firmware: {dev}'
assert dev.get('pending') is True, f'new device should be pending: {dev}'
assert dev.get('pins') == [4, 5, 12, 13], f'expected sorted pins from hello: {dev}'
assert dev.get('buttons') == {}, f'new device buttons should be empty: {dev}'
print(f'ok: devices={list(d)}')
"
assert_ha_alias "GET /api/home_intercom/devices — matches /devices" "${DEVICES}" "/api/home_intercom/devices"

# 9c. POST /record before approve — pending MAC → 403 (issue #51)
assert_http "POST /record — pending MAC → 403" \
    "$(fetch_code -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" \
        --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test")" "403"

# 9d. POST /devices/approve — then hello delivers config
APPROVE=$(fetch -X POST -H "Content-Type: application/json" \
    -d '{"mac": "AA:BB:CC:DD:EE:FF"}' "${URL}/api/home_intercom/devices/approve" || echo "")
assert_json "POST /api/home_intercom/devices/approve" "${APPROVE}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True, f'approve failed: {d}'
print(f'ok: approve={d}')
"
HELLO_OK=$(fetch -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" -H "Content-Type: application/json" \
    -d '{"firmware_version": "smoke-1.0"}' "${URL}/api/home_intercom/devices/hello" || echo "")
assert_json "POST /devices/hello after approve — status ok" "${HELLO_OK}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('status') == 'ok', f'expected ok after approve, got: {d}'
assert d.get('sample_rate') == 16000, f'bad sample_rate: {d}'
assert d.get('buttons') == {}, f'unconfigured buttons should be empty: {d}'
print(f'ok: hello={d}')
"

# 9e. GPIO → room map (issue #78)
MAP=$(fetch -X POST -H "Content-Type: application/json" \
    -d '{"mac": "AA:BB:CC:DD:EE:FF", "action": "buttons", "buttons": {"4": "test", "5": "mars"}}' \
    "${URL}/api/home_intercom/devices/manage" || echo "")
assert_json "POST /devices/manage action=buttons" "${MAP}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True, f'map failed: {d}'
assert d.get('buttons') == {'4': 'test'}, f'unknown rooms must be dropped: {d}'
print('ok')
"
HELLO_MAPPED=$(fetch -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" -H "Content-Type: application/json" \
    -d '{"firmware_version": "smoke-1.0"}' "${URL}/api/home_intercom/devices/hello" || echo "")
assert_json "POST /devices/hello — delivers GPIO map" "${HELLO_MAPPED}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('status') == 'ok', f'expected ok, got: {d}'
assert d.get('buttons') == {'4': 'test'}, f'hello should deliver mapped pins: {d}'
print(f'ok: hello={d}')
"

# 10–11. POST /record — MAC allow/deny (issue #47)
assert_http "POST /record — registered MAC → 200" \
    "$(fetch_code -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" \
        --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test")" "200"
assert_http "POST /record — unknown MAC → 403" \
    "$(fetch_code -X POST -H "X-Device-ID: 11:22:33:44:55:66" \
        --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test")" "403"

# 11b. POST /api/home_intercom/device/record — firmware path alias (issue #70)
assert_http "POST /api/home_intercom/device/record — registered MAC → 200" \
    "$(fetch_code -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" \
        --data-binary @"${TMPDIR}/test.wav" "${URL}/api/home_intercom/device/record?target=test")" "200"
assert_http "POST /api/home_intercom/device/record — unknown MAC → 403" \
    "$(fetch_code -X POST -H "X-Device-ID: 11:22:33:44:55:66" \
        --data-binary @"${TMPDIR}/test.wav" "${URL}/api/home_intercom/device/record?target=test")" "403"

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

# 13b. POST /devices/manage delete — store-only (no HA device registry)
MANAGE_DEL=$(fetch -X POST -H "Content-Type: application/json" \
    -d '{"mac": "AA:BB:CC:DD:EE:FF", "action": "delete"}' \
    "${URL}/api/home_intercom/devices/manage" || echo "")
assert_json "POST /api/home_intercom/devices/manage delete" "${MANAGE_DEL}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True and d.get('deleted') is True, f'bad delete: {d}'
print(f'ok: delete={d}')
"
DEVICES_AFTER=$(fetch "${URL}/devices" || echo "")
assert_json "GET /devices — MAC gone after delete" "${DEVICES_AFTER}" "
import sys, json
d = json.load(sys.stdin)
assert 'AA:BB:CC:DD:EE:FF' not in d, f'MAC still listed: {d}'
print('ok: store empty of deleted MAC')
"
if docker exec "${CONTAINER_NAME}" grep -q "AA:BB:CC:DD:EE:FF" /data/device_registry.json 2>/dev/null; then
    echo "  ❌ /data/device_registry.json still has deleted MAC"
    docker exec "${CONTAINER_NAME}" cat /data/device_registry.json 2>&1 || true
    exit 1
fi
echo "  ✅ device registry file no longer contains deleted MAC"
assert_http "POST /record after delete — unknown MAC → 403" \
    "$(fetch_code -X POST -H "X-Device-ID: AA:BB:CC:DD:EE:FF" \
        --data-binary @"${TMPDIR}/test.wav" "${URL}/record?target=test")" "403"

# 14–16. /chime — custom pre-announce (issue #66)
CHIME=$(fetch "${URL}/chime" || echo "")
assert_json "GET /chime — default" "${CHIME}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('custom') is False, f'expected default chime: {d}'
assert 'url' in d and 'default_url' in d, f'missing fields: {d}'
print(f'ok: chime={d}')
"

CHIME_POST=$(fetch -X POST --data-binary @"${TMPDIR}/test.wav" "${URL}/chime" || echo "")
assert_json "POST /chime — custom uploaded" "${CHIME_POST}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True, f'upload failed: {d}'
assert d.get('custom') is True, f'missing custom flag: {d}'
assert 'url' in d and 'custom_chime.wav' in d['url'], f'bad url: {d}'
print(f'ok: upload={d}')
"

CHIME_CUSTOM=$(fetch "${URL}/chime" || echo "")
assert_json "GET /chime — custom active" "${CHIME_CUSTOM}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('custom') is True, f'expected custom chime: {d}'
print('ok: custom active')
"

assert_http "GET /audio/custom_chime.wav" \
    "$(fetch_code "${URL}/audio/custom_chime.wav")" "200"

CHIME_DEL=$(fetch -X DELETE "${URL}/chime" || echo "")
assert_json "DELETE /chime — reset to default" "${CHIME_DEL}" "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok') is True and d.get('custom') is False, f'bad delete response: {d}'
print('ok: reset to default')
"

assert_ha_alias "GET /api/home_intercom/chime — matches /chime (default)" \
    "${CHIME}" "/api/home_intercom/chime"

echo "==> All Docker smoke tests passed! 🎉"
