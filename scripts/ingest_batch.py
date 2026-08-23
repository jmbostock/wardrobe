#!/usr/bin/env python3
"""Batch garment ingest — run a directory of photos through the app pipeline.

Mirrors exactly what the Wardrobe UI does per upload:
  validate → vision tag-read (VISION_ENGINE/VISION_URL) → create garment with
  category-aware defaults → save image (HEIC→JPEG, orientation, phash,
  color_sig) → near-dup note.

Run on the host that owns the data (during the 202→187 transition, that's 202;
afterwards 187). Point VISION_ENGINE/VISION_URL at the llama.cpp vision server.

Usage:
  VISION_ENGINE=llamacpp VISION_URL=http://127.0.0.1:28117 \
  DATA_DIR=$HOME/altacloset/data \
  /usr/bin/python3 scripts/ingest_batch.py /path/to/photos --email mazarrag@gmail.com
  # --dry-run: validate + tag-read only (no DB writes)
  # --no-ai: skip the vision tag-read (pixel-color + filename only)
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "webapp"))

from app import aifill, db, media  # noqa: E402
from app.media import (  # noqa: E402
    COLOR_HEX,
    WARDROBE_CATEGORIES,
    detect_color,
    normalize_color,
    save_garment_image,
    validate_image,
)
from app.store import wardrobe  # noqa: E402

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".avif"}


def _clean_name(stem: str) -> str:
    """Turn an iPhone-ish filename into a readable garment name."""
    s = stem.replace("_", " ").replace("-", " ").replace("  ", " ").strip()
    s = " ".join(s.split())
    return s[:200] or "Garment"


def resolve_user(email: str) -> int:
    conn = db.init()
    with db.lock():
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        raise SystemExit(f"no user with email: {email}")
    return row["id"]


MAX_PX = 1024  # match the app's MAX_UPLOAD_PX — large HEIC photos break vision


def _prepare_image(data: bytes, ext: str) -> tuple[bytes, str]:
    """HEIC→JPEG + downscale to ≤1024px so the vision model gets a sane image
    (giant phone photos make qwen2.5vl emit '????' garbage) and storage stays
    consistent with the app's upload path."""
    if ext not in ("heic", "jpg", "jpeg"):
        return data, ext
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if max(img.size) > MAX_PX:
            img.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=90)
        return buf.getvalue(), "jpg"
    except Exception:  # noqa: BLE001 — unreadable → keep and let save fail
        return data, ext


def ingest_one(user_id: int, path: Path, use_ai: bool, dry_run: bool = False) -> dict:
    data = path.read_bytes()
    try:
        ext = validate_image(data)
    except Exception as ex:  # noqa: BLE001
        return {"file": path.name, "ok": False, "error": f"not an image: {ex}"}
    data, ext = _prepare_image(data, ext)  # vision needs JPEG ≤1024px, not raw HEIC

    fields: dict = {}
    ai_err = None
    if use_ai:
        try:
            fields = aifill.ai_fill_garment(data) or {}
        except Exception as ex:  # noqa: BLE001 — vision down → degrade
            ai_err = str(ex)
            fields = {}

    name = (fields.get("name") or "").strip()[:200] or _clean_name(path.stem)
    category = (fields.get("category") or "").strip().lower()
    category = aifill.CATEGORY_SYNONYMS.get(category, category)
    if category not in WARDROBE_CATEGORIES:
        category = "top"
    color = normalize_color(fields.get("color") or "") or detect_color(data)
    color_hex = COLOR_HEX.get(color, "#8a8f98")
    brand = (fields.get("brand") or "").strip()[:120]
    sizes = (fields.get("sizes") or "").strip()[:200]

    result = {
        "file": path.name, "ok": True, "name": name, "category": category,
        "color": color, "brand": brand, "sizes": sizes, "ai_err": ai_err,
    }
    if dry_run:
        return result

    warmth, formality, occasions = 3, "casual", "casual"
    if category == "swimsuit":
        warmth, formality, occasions = 1, "casual", "active,beach"
    g = wardrobe.create(
        user_id, name, category, color_hex=color_hex, color_tags=color,
        brand=brand, sizes=sizes, owned=1, warmth=warmth,
        formality=formality, occasions=occasions,
    )
    save_garment_image(user_id, g.id, data, ext, ai_orient=use_ai)
    fresh = wardrobe.get(user_id, g.id)
    dup = None
    if fresh and fresh.phash:
        dup = media.nearest_dup(
            user_id, fresh.phash, color_sig=fresh.color_sig,
            exclude_id=fresh.id, category=fresh.category,
            color_tags=fresh.color_tags,
        )
    result.update({"id": g.id, "dup": dup.get("name") if dup else None})
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Batch ingest garment photos into a user's wardrobe")
    ap.add_argument("source", help="directory (or single file) of garment photos")
    ap.add_argument("--email", required=True, help="target user email")
    ap.add_argument("--dry-run", action="store_true", help="validate + tag-read only, no writes")
    ap.add_argument("--no-ai", action="store_true", help="skip vision tag-read")
    args = ap.parse_args()

    src = Path(args.source)
    if src.is_dir():
        files = sorted(p for p in src.iterdir() if p.suffix.lower() in IMG_EXTS and p.is_file())
    elif src.is_file() and src.suffix.lower() in IMG_EXTS:
        files = [src]
    else:
        raise SystemExit(f"source not found / not images: {src}")

    if not files:
        raise SystemExit("no image files found in source")
    user_id = resolve_user(args.email)
    print(f"[ingest] {len(files)} images → user {user_id} ({args.email}) "
          f"{'(DRY RUN)' if args.dry_run else ''}")

    ok = fail = 0
    t0 = time.time()
    for i, p in enumerate(files, 1):
        r = ingest_one(user_id, p, use_ai=not args.no_ai, dry_run=args.dry_run)
        if r.get("ok"):
            ok += 1
            tag = f"[{i}/{len(files)}] +#{r.get('id','-')} {r['name'][:38]}"
            if r.get("dup"):
                tag += f"  ⚠ dup→{r['dup'][:30]}"
            if r.get("ai_err"):
                tag += f"  (ai:{r['ai_err'][:24]})"
            print(tag)
        else:
            fail += 1
            print(f"[{i}/{len(files)}] ✗ {r['file']}: {r.get('error')}")
    print(f"\n[ingest] done ok={ok} fail={fail} in {time.time() - t0:.0f}s"
          + (" (DRY RUN — nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
