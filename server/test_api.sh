#!/usr/bin/env bash
# RaBbLE API smoke test
# Usage: ./test_api.sh [BASE_URL]
#   BASE_URL defaults to http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"

BASE="${1:-http://localhost:8000}"
PASS=0; FAIL=0

GREEN='\033[0;32m'; RED='\033[0;31m'; CYAN='\033[0;36m'; RESET='\033[0m'
pass() { PASS=$((PASS+1)); echo -e "${GREEN}✓${RESET} $*"; }
fail() { FAIL=$((FAIL+1)); echo -e "${RED}✗${RESET} $*"; }
hdr()  { echo; echo -e "${CYAN}── $* ──${RESET}"; }

echo "=== RaBbLE API smoke test → $BASE ==="

# ── Health ─────────────────────────────────────────────────────────────────────
hdr "Health"
HEALTH=$(curl -sf "$BASE/health" 2>/dev/null || true)
echo "  $HEALTH"
if echo "$HEALTH" | python3 -c "import sys,json; assert json.load(sys.stdin)['status']=='ok'" 2>/dev/null; then
  pass "Health check"
else
  fail "Health check — server not running at $BASE"
  exit 1
fi

# ── Register ───────────────────────────────────────────────────────────────────
hdr "Auth: register"
RAND="test_$(date +%s)"
REG=$(curl -sf -X POST "$BASE/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"Test User\",\"email\":\"$RAND@rabble.dev\"}")
echo "  $(echo "$REG" | python3 -c "import sys,json; d=json.load(sys.stdin); print('user_id:', d['user_id'], '| tier:', d['tier'])" 2>/dev/null)"
API_KEY=$(echo "$REG" | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])" 2>/dev/null || true)
[ -n "$API_KEY" ] && pass "Register → API key received" || { fail "Register failed: $REG"; exit 1; }

# Duplicate email rejected
DUP_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" -X POST "$BASE/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"Dup\",\"email\":\"$RAND@rabble.dev\"}" 2>/dev/null || echo "000")
[ "$DUP_STATUS" = "409" ] && pass "Duplicate email → 409" || fail "Duplicate email not rejected (HTTP $DUP_STATUS)"

# ── Token ──────────────────────────────────────────────────────────────────────
hdr "Auth: token"
TOK=$(curl -sf -X POST "$BASE/api/v1/auth/token" \
  -H "Content-Type: application/json" \
  -d "{\"api_key\":\"$API_KEY\"}")
JWT=$(echo "$TOK" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null || true)
[ -n "$JWT" ] && pass "Token exchange → JWT received" || { fail "Token failed: $TOK"; exit 1; }

# Bad key rejected
BAD_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" -X POST "$BASE/api/v1/auth/token" \
  -H "Content-Type: application/json" -d '{"api_key":"rbbl_invalid"}' 2>/dev/null || echo "000")
[ "$BAD_STATUS" = "401" ] && pass "Bad key → 401" || fail "Bad key not rejected (HTTP $BAD_STATUS)"

# ── Status ─────────────────────────────────────────────────────────────────────
hdr "Status"
STATUS=$(curl -sf "$BASE/api/v1/status" -H "Authorization: Bearer $JWT" 2>/dev/null || true)
echo "  $STATUS"
[ -n "$STATUS" ] && pass "Status endpoint" || fail "Status failed"

# ── Auth: X-API-Key header ─────────────────────────────────────────────────────
hdr "Auth: X-API-Key header"
STATUS_KEY=$(curl -sf "$BASE/api/v1/status" -H "X-API-Key: $API_KEY" 2>/dev/null || true)
[ -n "$STATUS_KEY" ] && pass "X-API-Key auth works" || fail "X-API-Key auth failed"

# Unauth blocked
UNAUTH=$(curl -sf -o /dev/null -w "%{http_code}" "$BASE/api/v1/status" 2>/dev/null || echo "000")
[ "$UNAUTH" = "401" ] && pass "No auth → 401" || fail "No auth not rejected (HTTP $UNAUTH)"

# ── Chat complete ──────────────────────────────────────────────────────────────
hdr "Chat (non-streaming)"
CHAT=$(curl -sf -X POST "$BASE/api/v1/chat/complete" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello RaBbLE, what are you?"}],"model_tier":"fast"}' \
  2>/dev/null || true)
CONTENT=$(echo "$CHAT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('content','')[:80])" 2>/dev/null || true)
[ -n "$CONTENT" ] && pass "Chat complete → response received" || fail "Chat complete failed: $CHAT"
echo "  Response snippet: $CONTENT..."

# ── Chat streaming ─────────────────────────────────────────────────────────────
hdr "Chat (streaming)"
STREAM=$(curl -sf -X POST "$BASE/api/v1/chat" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Name one idea to flourish today."}],"model_tier":"fast","workflow_type":"brainstorm"}' \
  2>/dev/null || true)
[ -n "$STREAM" ] && pass "Streaming chat → received ${#STREAM} chars" || fail "Streaming chat failed"

# ── Rate limit ─────────────────────────────────────────────────────────────────
# (Just verify the endpoint structure — actual limits need many rapid calls)
hdr "Bad model_tier rejected"
BAD_TIER=$(curl -sf -o /dev/null -w "%{http_code}" -X POST "$BASE/api/v1/chat/complete" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}],"model_tier":"turbo"}' 2>/dev/null || echo "000")
[ "$BAD_TIER" = "422" ] && pass "Invalid model_tier → 422" || fail "Invalid model_tier not rejected (HTTP $BAD_TIER)"

# ── Workflow ───────────────────────────────────────────────────────────────────
hdr "Workflows"
WF=$(curl -sf -X POST "$BASE/api/v1/workflows" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"goal":"Build a 10-minute morning routine","workflow_type":"thrive","model_tier":"fast"}' \
  2>/dev/null || true)
WF_ID=$(echo "$WF" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || true)
WF_STATE=$(echo "$WF" | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])" 2>/dev/null || true)
[ -n "$WF_ID" ] && pass "Workflow created → state=$WF_STATE" || { fail "Workflow create failed: $WF"; }

if [ -n "$WF_ID" ]; then
  # Step
  STEP=$(curl -sf -X POST "$BASE/api/v1/workflows/$WF_ID/step" \
    -H "Authorization: Bearer $JWT" \
    -H "Content-Type: application/json" \
    -d '{"message":"I have 15 minutes and prefer calm activities."}' \
    2>/dev/null || true)
  STEP_STATE=$(echo "$STEP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state','?'))" 2>/dev/null || true)
  [ -n "$STEP_STATE" ] && pass "Workflow step → state=$STEP_STATE" || fail "Workflow step failed: $STEP"

  # Get
  GET=$(curl -sf "$BASE/api/v1/workflows/$WF_ID" \
    -H "Authorization: Bearer $JWT" 2>/dev/null || true)
  STEP_COUNT=$(echo "$GET" | python3 -c "import sys,json; print(json.load(sys.stdin).get('step_count',0))" 2>/dev/null || true)
  [ -n "$STEP_COUNT" ] && pass "Workflow GET → step_count=$STEP_COUNT" || fail "Workflow GET failed"

  # Cancel
  DEL=$(curl -sf -X DELETE "$BASE/api/v1/workflows/$WF_ID" \
    -H "Authorization: Bearer $JWT" 2>/dev/null || true)
  DEL_STATUS=$(echo "$DEL" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || true)
  [ "$DEL_STATUS" = "cancelled" ] && pass "Workflow cancelled" || fail "Workflow cancel failed: $DEL"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo
echo "──────────────────────────────────"
echo -e "  ${GREEN}Passed${RESET}: $PASS  ${RED}Failed${RESET}: $FAIL"
echo "──────────────────────────────────"
[ "$FAIL" -gt 0 ] && exit 1
exit 0
