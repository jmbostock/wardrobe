#!/usr/bin/env python3
"""One-off: re-check existing garments' orientation with the 4-way tag reader
(0/90/180/270) and re-save + re-tag any that are sideways or upside-down.

Useful after adding 90/270 support: photos imported when only 180° flips were
handled (or that were blindly rotated by the old portrait heuristic) can leave
sideways items (e.g. shorts) in the wardrobe. This pass finds them via the tag
reader, rotates them upright, and re-reads metadata (name/category/color/…)
from the now-upright image so labels match what you see.

Runs inside the webapp container (has the app code + vision config), on the
host that owns the data — currently 187:

  docker exec -i altacloset-webapp python - < scripts/fix_orientation.py \
      --email mazarrag@gmail.com --dry-run      # preview only
  docker exec -i altacloset-webapp python - < scripts/fix_orientation.py \
      --email mazarrag@gmail.com                # apply

Flags:
  --email     target user (required)
  --category  only check this category, e.g. "bottom" (default: all)
  --dry-run   detect only, make no changes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() else None
if ROOT is not None:  # running from a checkout; the container already has /app
    sys.path.insert(0, str(ROOT / "services" / "webapp"))

from app import aifill, db, media  # noqa: E402
from app.media import (  # noqa: E402
    COLOR_HEX,
    WARDROBE_CATEGORIES,
    detect_color,
    normalize_color,
    save_garment_image,
)
from app.store import wardrobe  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Fix rotated garment photos + re-tag")
    ap.add_argument("--email", required=True)
    ap.add_argument("--category", default="", help="only this category (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = db.init()
    uid = conn.execute("SELECT id FROM users WHERE email=?", (args.email,)).fetchone()
    if not uid:
        sys.exit(f"no user with email: {args.email}")
    uid = uid["id"]

    q = "SELECT id, category, name FROM garments WHERE user_id=?"
    params: list = [uid]
    if args.category:
        q += " AND category=?"
        params.append(args.category)
    rows = conn.execute(q, params).fetchall()

    scanned = fixed = 0
    for r in rows:
        gid = r["id"]
        p = media.garment_image_path(uid, gid)
        if p is None:
            continue
        scanned += 1
        data = p.read_bytes()
        rot = aifill.ai_orientation(data)
        if rot == 0:
            continue
        print(f"fix gid={gid} [{r['category']}] {r['name'][:30]:30} rotate={rot}")
        if args.dry_run:
            continue
        ext = p.suffix.lstrip(".") or "jpg"
        save_garment_image(uid, gid, data, ext, ai_orient=False, rotate=rot)
        # re-read metadata from the now-upright image so labels are correct
        oriented = media.garment_image_path(uid, gid)
        if oriented is None:
            fixed += 1
            continue
        try:
            fields = aifill.ai_fill_garment(oriented.read_bytes()) or {}
        except Exception:  # noqa: BLE001 — vision hiccup → keep old labels
            fields = {}
        upd: dict = {}
        if fields.get("name"):
            upd["name"] = fields["name"].strip()[:200]
        if fields.get("category"):
            cat = fields["category"].strip().lower()
            cat = aifill.CATEGORY_SYNONYMS.get(cat, cat)
            if cat in WARDROBE_CATEGORIES:
                upd["category"] = cat
        if fields.get("color"):
            col = normalize_color(fields["color"]) or detect_color(oriented.read_bytes())
            upd["color_tags"] = col
            upd["color_hex"] = COLOR_HEX.get(col, "#8a8f98")
        if fields.get("brand"):
            upd["brand"] = fields["brand"].strip()[:120]
        if fields.get("sizes"):
            upd["sizes"] = fields["sizes"].strip()[:200]
        if upd:
            wardrobe.update(uid, gid, **upd)
        fixed += 1

    print(f"\n[fix] scanned={scanned} rotated(+retagged)={fixed}"
          + (" (DRY RUN — nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
