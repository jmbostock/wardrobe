#!/usr/bin/env python3
"""Machine-driven color backfill for existing garments.

Re-derives the color of garments whose stored color is a COARSE canonical bucket
("blue", "green", "red", ...) or blank — using the garment PHOTO's pixels via
`media.refine_color` / `media.detect_color`. A specific tag color ("navy",
"black", "white", ...) is never overridden: photos read differently than tags
(a navy shirt photographs bright blue; a black dress photographs warm), so we
only refine where the machine has to guess anyway.

Run INSIDE the webapp container (needs app code + data):
    docker cp scripts/backfill_colors.py altacloset-webapp:/tmp/backfill_colors.py
    docker exec -w /app -e PYTHONPATH=/app altacloset-webapp \
        python /tmp/backfill_colors.py [--apply] [--user 3]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("DATA_DIR", "/data")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import media  # noqa: E402
from app.store import wardrobe  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write changes to the DB (default: dry-run)")
    ap.add_argument("--user", type=int, default=3,
                    help="user id (default 3 = bostock@gmail.com)")
    args = ap.parse_args()

    refinable = set(media.COLOR_REFINE_FAMILIES)
    changed = 0
    for g in wardrobe.all(args.user):
        cur = (g.color_tags or "").split(",")[0].strip()
        cur = media.normalize_color(cur)
        # only coarse/blank colors are machine-reassignable
        if cur not in refinable and cur != "":
            continue
        img_path = media.garment_image_path(args.user, g.id)
        if not img_path:
            continue
        data = img_path.read_bytes()
        new = media.refine_color(cur, data) or media.detect_color(data)
        if not new or new == cur:
            continue
        changed += 1
        if args.apply:
            wardrobe.update(
                args.user, g.id,
                color_tags=new,
                color_hex=media.COLOR_HEX.get(new, "#8a8f98"),
            )
        print(f"#{g.id} {g.name[:40]!r}: {cur or '(blank)'} -> {new}  "
              f"[{'APPLIED' if args.apply else 'dry-run'}]")

    if changed:
        print(f"{changed} garment(s) would change — run with --apply to write."
              if not args.apply else f"{changed} garment(s) updated.")
    else:
        print("No coarse/blank colors to re-derive.")


if __name__ == "__main__":
    main()
