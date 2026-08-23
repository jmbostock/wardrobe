#!/usr/bin/env bash
# rec_weekly.sh — Clueless Closet weekly rec-engine refresh (runs on 202, GPU host).
#
# Orchestrates the learning loop between the two hosts:
#   187 (primary) holds the live wardrobe + interaction log (webapp, always up).
#   202 (GPU)     runs FashionCLIP embedding + implicit ALS training.
#
#   pull  : snapshot 187's live DB (SQLite online backup — webapp stays up) and
#           copy any new garment images to 202.
#   build : scripts/rec_build.py  → embed new garments (FashionCLIP) + retrain
#           ALS (auto-skips below 20 non-'shown' interactions, so no model is
#           written on an empty trickle).
#   push  : upsert the fresh garment_embeddings rows into 187's DB (never loses
#           interactions that landed mid-run) and copy rec/als.npz to 187.
#
# Runs from rec-retrain.timer (systemd user timer, weekly). Manual:
#     scripts/rec_weekly.sh
set -euo pipefail

PRIMARY="bostock@10.0.1.187"
DATA="$HOME/altacloset/data"
REC="$DATA/rec"
LOG="$HOME/altacloset/logs/rec_weekly.log"

mkdir -p "$(dirname "$LOG")" "$REC"
exec >>"$LOG" 2>&1
echo "== $(date -Is) rec_weekly start =="

# --- 1) pull: snapshot 187's live DB + copy any new garment images -----------
ssh -o ConnectTimeout=20 "$PRIMARY" \
  'python3 -c "import sqlite3; s=sqlite3.connect(\"/home/bostock/altacloset/data/db/altacloset.db\"); d=sqlite3.connect(\"/tmp/altacloset.db.snap\"); s.backup(d); d.close(); s.close()"' \
  && scp -o ConnectTimeout=20 "$PRIMARY:/tmp/altacloset.db.snap" "$DATA/db/altacloset.db" \
  && ssh -o ConnectTimeout=20 "$PRIMARY" 'rm -f /tmp/altacloset.db.snap'
ssh -o ConnectTimeout=20 "$PRIMARY" 'cd ~/altacloset/data && tar czf - wardrobe uploads' \
  | tar xzf - -C "$DATA"

# --- 2) embed + retrain on the GPU (train auto-skips below 20 interactions) ---
cd "$HOME/altacloset"
export HF_HOME=/mnt/models/altacloset/rec/hf
export DATA_DIR="$DATA"
export OPENBLAS_NUM_THREADS=1
/usr/bin/python3 scripts/rec_build.py

# --- 3) push back: fresh embeddings (upsert) + als.npz -> 187 -----------------
if [ -f "$REC/als.npz" ]; then
  ssh -o ConnectTimeout=20 "$PRIMARY" 'mkdir -p ~/altacloset/data/rec'
  scp -o ConnectTimeout=20 "$REC/als.npz" "$PRIMARY:~/altacloset/data/rec/als.npz"
fi
scp -o ConnectTimeout=20 "$DATA/db/altacloset.db" "$PRIMARY:/tmp/altacloset.db.from202"
ssh -o ConnectTimeout=20 "$PRIMARY" 'python3 - <<PY
import sqlite3
src = sqlite3.connect("/tmp/altacloset.db.from202")
dst = sqlite3.connect("/home/bostock/altacloset/data/db/altacloset.db")
cols = [r[1] for r in src.execute("PRAGMA table_info(garment_embeddings)").fetchall()]
ph = ",".join("?" * len(cols))
q = "INSERT OR REPLACE INTO garment_embeddings (%s) VALUES (%s)" % (",".join(cols), ph)
n = 0
for row in src.execute("SELECT %s FROM garment_embeddings" % ",".join(cols)).fetchall():
    dst.execute(q, row)
    n += 1
dst.commit()
print("merged %d embeddings into 187" % n)
src.close()
dst.close()
PY
rm -f /tmp/altacloset.db.from202'

echo "== $(date -Is) rec_weekly done =="
