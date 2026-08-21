#!/usr/bin/env bash
# Migrate altacloset from the test host (202) to the target machine (same GPU).
#
# Usage:
#   scripts/migrate-to-target.sh <user@host> [remote_path]
#   e.g. scripts/migrate-to-target.sh bostock@10.0.1.199 /opt/altacloset
#
# What it does:
#   1. rsync code + compose + .env (excludes data/ and .git)
#   2. rsync data/ (models, wardrobe, db) so the target does NOT re-download ~6GB
#   3. prints the target-side bring-up + smoke-test commands
set -euo pipefail

SRC_HOST="${1:?usage: migrate-to-target.sh <user@host> [remote_path]}"
REMOTE_PATH="${2:-/opt/altacloset}"
THIS_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> [1/3] syncing code / compose / .env -> $SRC_HOST:$REMOTE_PATH"
rsync -av --delete \
  --exclude data --exclude .git --exclude __pycache__ \
  "$THIS_DIR/" "$SRC_HOST:$REMOTE_PATH/"

echo ">> [2/3] syncing data/ (models, wardrobe, db) — no re-download needed"
ssh "$SRC_HOST" "mkdir -p $REMOTE_PATH"
rsync -av "$THIS_DIR/data/" "$SRC_HOST:$REMOTE_PATH/data/"

cat <<EOF

>> [3/3] migration staged. On the target machine:

    ssh $SRC_HOST
    cd $REMOTE_PATH
    # check .env values (ports, weather source, HA creds) match this host
    docker compose up -d --build
    docker compose ps

  Smoke test (docs/host-202-notes.md §3):
    curl -s http://127.0.0.1:\${WEBAPP_PORT:-28082}/health
    curl -s http://127.0.0.1:28082/api/weather
    curl -s -X POST http://127.0.0.1:28082/api/recommend \\
         -H 'Content-Type: application/json' -d '{"activity":"office"}'
EOF
