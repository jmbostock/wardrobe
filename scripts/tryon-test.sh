#!/usr/bin/env bash
# Smoke-test POST /api/tryon against a running altacloset webapp + ComfyUI.
# Usage: scripts/tryon-test.sh <person_image> <garment_id> [base_url] [email] [password]
set -euo pipefail

PERSON="${1:?usage: tryon-test.sh <person_image> <garment_id> [base_url] [email] [password]}"
GARMENT_ID="${2:?garment id}"
BASE="${3:-http://127.0.0.1:28085}"
EMAIL="${4:-me@example.com}"
PASS="${5:-test-pass-123}"

TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")
[ -n "$TOKEN" ] || { echo "login failed (wrong email/password?)"; exit 1; }

echo ">> try-on garment $GARMENT_ID on $PERSON (this can take 30-90s first run: model download)"
echo ">> curl -s -X POST $BASE/api/tryon -F garment_id=$GARMENT_ID -F person=@$PERSON"
time curl -s -X POST "$BASE/api/tryon" \
  -H "Authorization: Bearer $TOKEN" \
  -F "garment_id=$GARMENT_ID" -F "person=@$PERSON" -o /tmp/tryon-result.json

echo
echo ">> response:"; cat /tmp/tryon-result.json; echo
URL=$(python3 -c "import json;print(json.load(open('/tmp/tryon-result.json')).get('result_url',''))" 2>/dev/null || true)
if [ -n "$URL" ]; then
  echo ">> fetching result image $URL"
  curl -s -H "Authorization: Bearer $TOKEN" "$BASE$URL" -o /tmp/tryon-result.png
  ls -la /tmp/tryon-result.png
fi
