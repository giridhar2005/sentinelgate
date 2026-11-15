#!/usr/bin/env bash
set -euo pipefail

echo "=========================================================="
echo "Phase 4 Acceptance Test: Clean Wheel Installation Smoke Test"
echo "=========================================================="

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Step 1: Clean and build fresh wheel
echo "1. Building distribution wheel..."
rm -rf dist/ build/ *.egg-info
uv build --wheel

WHEEL_FILE=$(ls dist/sentinelgate-*.whl | head -n 1)
echo "Built wheel: $WHEEL_FILE"

# Step 2: Create isolated temp directory and virtualenv
TMP_DIR=$(mktemp -d -t sg_pkg_test_XXXXXX)
GATEWAY_PID=""
trap 'echo "Cleaning up temp dir: $TMP_DIR"; [ -n "${GATEWAY_PID:-}" ] && kill "$GATEWAY_PID" 2>/dev/null || true; rm -rf "$TMP_DIR"' EXIT

echo "2. Creating isolated venv in $TMP_DIR..."
uv venv "$TMP_DIR/venv"
uv pip install --python "$TMP_DIR/venv" pip
"$TMP_DIR/venv/bin/pip" install "$REPO_ROOT/$WHEEL_FILE"

# Step 3: Switch outside the repo to test isolation
cd "$TMP_DIR"
echo "Working directory is outside repo: $(pwd)"

# Step 4: Test CLI help
echo "3. Testing CLI entrypoint..."
"$TMP_DIR/venv/bin/sentinelgate" --help > /dev/null
echo "✓ sentinelgate --help succeeded"

# Step 5: Start gateway in background in demo mode on custom port
TEST_PORT=8973
echo "4. Starting sentinelgate on port $TEST_PORT..."
"$TMP_DIR/venv/bin/sentinelgate" --demo --port "$TEST_PORT" &
GATEWAY_PID=$!
echo "Gateway PID: $GATEWAY_PID"

# Wait for server to boot
for i in {1..30}; do
    if curl -s -o /dev/null "http://127.0.0.1:$TEST_PORT/health"; then
        echo "Gateway is up after $i seconds."
        break
    fi
    sleep 0.5
done

# Step 6: Hit /health and assert 200
echo "5. Verifying /health..."
HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$TEST_PORT/health")
echo "GET /health status: $HEALTH_STATUS"
if [ "$HEALTH_STATUS" != "200" ]; then
    echo "ERROR: /health returned $HEALTH_STATUS, expected 200"
    exit 1
fi
echo "✓ /health returned 200 OK"

# Step 7: Hit /dashboard and assert 200
echo "6. Verifying /dashboard..."
DASHBOARD_RESP=$(curl -s -w "\nHTTP_STATUS:%{http_code}" "http://127.0.0.1:$TEST_PORT/dashboard")
DASHBOARD_STATUS=$(echo "$DASHBOARD_RESP" | grep "HTTP_STATUS:" | cut -d':' -f2)
DASHBOARD_BODY=$(echo "$DASHBOARD_RESP" | grep -v "HTTP_STATUS:")

echo "GET /dashboard status: $DASHBOARD_STATUS"
if [ "$DASHBOARD_STATUS" != "200" ]; then
    echo "ERROR: /dashboard returned $DASHBOARD_STATUS, expected 200"
    exit 1
fi

if [[ "$DASHBOARD_BODY" != *"SentinelGate"* && "$DASHBOARD_BODY" != *"<!DOCTYPE html>"* ]]; then
    echo "ERROR: /dashboard did not contain expected HTML template content"
    exit 1
fi
echo "✓ /dashboard returned 200 OK with packaged HTML content"

# Step 8: Clean shutdown
echo "7. Terminating gateway PID $GATEWAY_PID..."
kill "$GATEWAY_PID" || true
wait "$GATEWAY_PID" 2>/dev/null || true
unset GATEWAY_PID

echo "=========================================================="
echo "Phase 4 Acceptance Test: ALL CHECKS PASSED SUCCESSFULLY!"
echo "=========================================================="
