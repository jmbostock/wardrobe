"""Re-process every stored garment photo with the new 'look at it, then rotate'
orientation pipeline (ai_orient=True). The vision model reports which image
edge the garment's top is on and we rotate it upright — so the sideways
flat-lay photos (top on the left) get rotated to match the upright ones.

Run INSIDE the webapp container (has app deps + /data):
    docker exec -w /app -e PYTHONPATH=/app altacloset-webapp \
        python /tmp/backfill_orientation.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from app import aifill, media  # noqa: E402
from app.db import init  # noqa: E402
from app.store import wardrobe  # noqa: E402


def main() -> None:
    conn = init()
    rows = conn.execute("SELECT DISTINCT user_id FROM garments").fetchall()
    rotated, unchanged, skipped = [], [], 0
    for (uid,) in rows:
        for g in wardrobe.all(uid):
            p = media.garment_image_path(uid, g.id)
            if p is None:
                continue
            before = p.name
            ext = p.suffix.lstrip(".") or "jpg"
            media.save_garment_image(uid, g.id, p.read_bytes(), ext, ai_orient=True)
            after = media.garment_image_path(uid, g.id)
            if after is None:
                skipped += 1
            elif after.name != before:
                rotated.append((g.id, before, after.name))
            else:
                unchanged.append(g.id)
            time.sleep(0.4)  # pace vision-model calls
    print(f"users={len(rows)} rotated={len(rotated)} unchanged={len(unchanged)} skipped={skipped}")
    for gid, b, a in rotated:
        print(f"  garment {gid}: {b} -> {a}")

    # sanity check: the detector should now call every photo upright (rot 0)
    bad: list[tuple[int, str, int | None]] = []
    for (uid,) in rows:
        for g in wardrobe.all(uid):
            p = media.garment_image_path(uid, g.id)
            if p is None:
                continue
            r = aifill.ai_upright_rotation(p.read_bytes())
            if r not in (0, None):
                bad.append((g.id, p.name, r))
            time.sleep(0.4)
    print("still-not-upright:", bad if bad else "none")


if __name__ == "__main__":
    main()
