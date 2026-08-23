"""Re-process every stored garment photo with the deterministic orientation
pipeline (ai_orient=False): EXIF righting + a HARD guarantee that the saved
image is portrait (never horizontal/landscape).

This reverts any photo that a previous (removed) edge-detection pass rotated
into a landscape frame — e.g. #119/#122/#124 were turned 1024x768 and must go
back to portrait. It also re-encodes every image and refreshes phash/color_sig.

Run INSIDE the webapp container (has app deps + /data):
    docker exec -w /app -e PYTHONPATH=/app altacloset-webapp \
        python /tmp/backfill_orientation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/app")

from app import media  # noqa: E402
from app.db import init  # noqa: E402
from app.store import wardrobe  # noqa: E402


def main() -> None:
    from PIL import Image
    conn = init()
    rows = conn.execute("SELECT DISTINCT user_id FROM garments").fetchall()
    landscape_before, landscape_after, count = [], [], 0
    for (uid,) in rows:
        for g in wardrobe.all(uid):
            p = media.garment_image_path(uid, g.id)
            if p is None:
                continue
            count += 1
            media.save_garment_image(uid, g.id, p.read_bytes(),
                                     p.suffix.lstrip(".") or "jpg", ai_orient=False)
            q = media.garment_image_path(uid, g.id)
            if q is not None:
                im = Image.open(q)
                if im.width > im.height:
                    landscape_after.append(g.id)
    # report landscape files that existed before (read-only) vs after
    pre = []
    for (uid,) in rows:
        for g in wardrobe.all(uid):
            p = media.garment_image_path(uid, g.id)
            if p is not None:
                im = Image.open(p)
                if im.width > im.height:
                    pre.append(g.id)
    print(f"users={len(rows)} garments_with_image={count}")
    print("landscape before re-normalize:", pre if pre else "none")
    print("landscape AFTER re-normalize:", landscape_after if landscape_after else "none")


if __name__ == "__main__":
    main()
