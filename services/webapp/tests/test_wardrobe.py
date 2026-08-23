"""Wardrobe create/image/delete module tests — runnable without pytest:
    python services/webapp/tests/test_wardrobe.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-wardrobe-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, wardrobe  # noqa: E402
from app import imglink  # noqa: E402
from app.tryon import _load_garment_image  # noqa: E402

w = wardrobe.Wardrobe()

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # png magic + padding


def test_create_upload_serve_delete():
    ua = auth.create_user("w1@example.com", "password123")
    g = w.create(ua["id"], "Test tee", "top", color_hex="#aabbcc", color_tags="gray")
    assert g.id > 0 and g.name == "Test tee" and g.category == "top"

    # save an image for the garment (mirrors main._save_garment_image)
    data_dir = os.environ["DATA_DIR"]
    d = Path(data_dir) / "wardrobe" / str(ua["id"])
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{g.id}.png"
    p.write_bytes(PNG)
    assert w.update_image(ua["id"], g.id, p.name) is True

    # tryon loader finds it via glob (in-memory g has no image_path yet)
    assert _load_garment_image(g, ua["id"]) == PNG
    # ...and via recorded image_path after reload
    g2 = w.get(ua["id"], g.id)
    assert g2 is not None and g2.image_path == p.name
    assert _load_garment_image(g2, ua["id"]) == PNG

    assert w.delete(ua["id"], g.id) is True
    assert w.get(ua["id"], g.id) is None


def test_rotate_180_flips_and_stays_portrait():
    """Manual 180° rotate (upside-down fix) flips the photo in place, stays
    portrait (never horizontal), and re-saves through save_garment_image so
    phash / color_sig stay consistent."""
    import io as _io

    from PIL import Image

    from app.media import garment_image_path, save_garment_image
    from app.routes.wardrobe_routes import rotate_garment_image

    ua = auth.create_user("wRot@example.com", "password123")
    g = w.create(ua["id"], "Upside-down top", "top")
    # portrait image with a DARK band at the TOP so orientation is measurable
    img = Image.new("RGB", (800, 1200), (200, 200, 200))
    for x in range(800):
        for y in range(0, 300):
            img.putpixel((x, y), (20, 20, 20))
    buf = _io.BytesIO()
    img.save(buf, "PNG")
    save_garment_image(ua["id"], g.id, buf.getvalue(), "png")

    def dark_band_at_top() -> bool:
        data = garment_image_path(ua["id"], g.id).read_bytes()
        im = Image.open(_io.BytesIO(data)).convert("RGB")
        top = sum(im.getpixel((400, y))[0] for y in range(0, 100)) / 100
        bot = sum(im.getpixel((400, y))[0] for y in range(1100, 1200)) / 100
        return top < bot

    assert dark_band_at_top(), "precondition: dark band should be at the top"
    d = rotate_garment_image(g.id, {"id": ua["id"]})
    assert d["has_image"] is True
    assert not dark_band_at_top(), "after 180° the dark band should be at the bottom"
    # still a valid portrait image on disk
    im = Image.open(_io.BytesIO(garment_image_path(ua["id"], g.id).read_bytes()))
    assert im.height > im.width, im.size
    # cross-user cannot rotate someone else's garment
    ub = auth.create_user("wRot2@example.com", "password123")
    from fastapi import HTTPException
    try:
        rotate_garment_image(g.id, {"id": ub["id"]})
        raise AssertionError("expected 404 for cross-user rotate")
    except HTTPException as ex:
        assert ex.status_code == 404


def test_cross_user_isolation():
    ua = auth.create_user("wA@example.com", "password123")
    ub = auth.create_user("wB@example.com", "password123")
    g = w.create(ua["id"], "Mine", "top")
    assert w.get(ub["id"], g.id) is None
    assert w.update_image(ub["id"], g.id, "x.png") is False
    assert w.delete(ub["id"], g.id) is False
    assert w.get(ua["id"], g.id) is not None


def test_create_brand_sizes():
    ua = auth.create_user("wB2@example.com", "password123")
    g = w.create(ua["id"], "Old Navy tee", "top", brand="Old Navy", sizes="S,M,L")
    assert g.brand == "Old Navy" and g.sizes == "S,M,L"
    g2 = w.get(ua["id"], g.id)
    assert g2.brand == "Old Navy" and g2.sizes == "S,M,L"
    # defaults are empty strings
    g3 = w.create(ua["id"], "Plain tee", "top")
    assert g3.brand == "" and g3.sizes == ""


def test_update():
    ua = auth.create_user("wU@example.com", "password123")
    g = w.create(ua["id"], "Old name", "top", color_hex="#1f2a44", color_tags="navy")
    assert w.update(ua["id"], g.id, name="New name", category="dress",
                    color_hex="#1a1a1a", color_tags="black") is True
    g2 = w.get(ua["id"], g.id)
    assert g2 is not None and g2.name == "New name" and g2.category == "dress"
    assert g2.color_tags == "black"
    # brand + sizes can be set on update
    assert w.update(ua["id"], g.id, brand="Express", sizes="2,4,6") is True
    g3 = w.get(ua["id"], g.id)
    assert g3.brand == "Express" and g3.sizes == "2,4,6"
    # rating defaults to 0, and can be updated out of 10
    assert g2.rating == 0
    assert w.update(ua["id"], g.id, rating=8) is True
    assert w.get(ua["id"], g.id).rating == 8
    # cross-user isolation + no-op
    ub = auth.create_user("wU2@example.com", "password123")
    assert w.update(ub["id"], g.id, name="nope") is False
    assert w.update(ua["id"], g.id) is False


def test_owned_flag():
    ua = auth.create_user("wOwn@example.com", "password123")
    # defaults to owned
    g = w.create(ua["id"], "My jacket", "outerwear")
    assert g.owned == 1
    # can create as a to-buy / wishlist item
    g2 = w.create(ua["id"], "Dream coat", "outerwear", owned=0)
    assert g2.owned == 0
    # can flip ownership on update
    assert w.update(ua["id"], g.id, owned=0) is True
    assert w.get(ua["id"], g.id).owned == 0
    assert w.update(ua["id"], g.id, owned=1) is True
    assert w.get(ua["id"], g.id).owned == 1


def test_list_wardrobe_used_count():
    """used_count counts how many saved outfits reference each garment."""
    from app.outfits import OutfitStore
    from app.routes.wardrobe_routes import list_wardrobe

    ua = auth.create_user("wUsed@example.com", "password123")
    g1 = w.create(ua["id"], "Used tee", "top")
    g2 = w.create(ua["id"], "Unused pants", "bottom")
    outfits = OutfitStore()
    outfits.create(ua["id"], "Outfit A", [g1.id])
    outfits.create(ua["id"], "Outfit B", [g1.id])
    outfits.create(ua["id"], "Outfit C", [g1.id, g2.id])

    items = list_wardrobe({"id": ua["id"]})
    by_id = {d["id"]: d for d in items}
    assert by_id[g1.id]["used_count"] == 3, by_id[g1.id]
    assert by_id[g2.id]["used_count"] == 1, by_id[g2.id]
    # new garments default to created_at so "Newest" sorting has something to use
    assert by_id[g1.id]["created_at"], by_id[g1.id]


def test_prep_person_preserves_head_and_exif():
    """CatVTON center-crops to 768x1024; _prep_person letterboxes first so the
    crop is a no-op (head never cut) and EXIF-rotated photos are righted."""
    from app import tryon
    from PIL import Image as PILImage
    import io as _io

    # tall portrait (576x1246) — must letterbox to 3:4, not crop the top
    buf = _io.BytesIO()
    PILImage.new("RGB", (576, 1246), (180, 60, 60)).save(buf, "PNG")
    out = tryon._prep_person(buf.getvalue())
    img = PILImage.open(_io.BytesIO(out))
    assert img.size == (768, 1024), img.size

    # EXIF orientation 6 (stored landscape, meant to be portrait) — must be
    # righted so the person isn't sideways (which distorts proportions)
    base = PILImage.new("RGB", (1000, 500), (60, 60, 180))
    exif = PILImage.Exif()
    exif[0x0112] = 6
    buf2 = _io.BytesIO()
    base.save(buf2, "JPEG", exif=exif)
    out2 = tryon._prep_person(buf2.getvalue())
    img2 = PILImage.open(_io.BytesIO(out2))
    assert img2.size == (768, 1024), img2.size


def test_imglink_product_gallery_preferred_over_logo():
    # og:image is a logo; product-gallery <img> alt pattern should win
    html = """
    <html><head><meta property="og:image" content="https://shop.com/logo.png" /></head>
    <body>
      <img src="https://shop.com/nav-logo.svg" alt="Shop logo" />
      <img width="390" src="https://cdn.shop.com/products/pants-front.png?width=737"
           alt="Image number 1 showing, High-Waisted Pants" />
      <img width="390" src="https://cdn.shop.com/products/pants-back.png?width=737"
           alt="Image number 2 showing, High-Waisted Pants" />
    </body></html>
    """
    got = imglink.extract_image_url_from_html(html)
    assert got == "https://cdn.shop.com/products/pants-front.png?width=737", got


def test_imglink_jsonld_product_image():
    # JSON-LD product image preferred over a logo og:image
    html = """
    <html><head>
      <meta property="og:image" content="https://shop.com/logo.png" />
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Product","name":"Tee",
       "image":["https://cdn.shop.com/p1.jpg","https://cdn.shop.com/p2.jpg"]}
      </script>
    </head></html>
    """
    got = imglink.extract_image_url_from_html(html)
    assert got == "https://cdn.shop.com/p1.jpg", got


def test_imglink_og_image_last_resort_and_byte_detection():
    html = '<html><head><meta property="og:image" content="https://x.com/img.jpg" /></head><body><p>hi</p></body></html>'
    assert imglink.extract_image_url_from_html(html) == "https://x.com/img.jpg"
    assert imglink.extract_image_url_from_html("<html><body>no images</body></html>") is None

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    assert imglink.is_image_bytes(png) is True
    assert imglink.detect_ext(png) == "png"
    assert imglink.is_image_bytes(b"<html>not an image</html>") is False


def test_extract_product_page():
    html = """
    <html><head>
      <title>High-Waisted Pants | Old Navy</title>
      <meta property="og:title" content="High-Waisted Pants | Old Navy" />
      <meta property="og:image" content="https://shop.com/logo.png" />
      <meta name="description" content="Classic high-waisted blue jeans" />
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Product","name":"High-Waisted Pants",
       "brand":{"@type":"Brand","name":"Old Navy"},
       "color":"Blue","size":"29,30,31",
       "image":["//cdn.shop.com/pants-front.png","//cdn.shop.com/pants-back.png"]}
      </script>
    </head><body>
      <img src="https://shop.com/nav-logo.svg" alt="logo" />
      <img src="//cdn.shop.com/alt-1.png" alt="Image number 1 showing, High-Waisted Pants" />
      <img src="//cdn.shop.com/alt-2.png" alt="Image number 2 showing, High-Waisted Pants" />
    </body></html>
    """
    info = imglink.extract_product_page(html)
    assert info["name"] == "High-Waisted Pants", info
    assert info["color"].lower() == "blue", info
    assert info["category"] == "bottom", info
    assert info["brand"] == "Old Navy", info
    assert "29" in info["sizes"] and "30" in info["sizes"] and "31" in info["sizes"], info
    # JSON-LD images first, then gallery alt images, then product-ish; no logo
    assert "//cdn.shop.com/pants-front.png" in info["images"]
    assert "//cdn.shop.com/pants-back.png" in info["images"]
    assert "logo" not in " ".join(info["images"]).lower()


def test_extract_product_page_brand_and_sizes_from_html():
    # no JSON-LD: brand from og:site_name, sizes from a <select> size picker
    html = """
    <html><head>
      <meta property="og:site_name" content="Express" />
      <meta property="og:title" content="Sheath Dress" />
    </head><body>
      <label for="size-select">Size</label>
      <select id="size-select">
        <option value="0">0</option><option value="2">2</option><option value="4">4</option>
      </select>
      <img src="//cdn.com/dress-front.jpg" alt="Image number 1 showing, Sheath Dress" />
    </body></html>
    """
    info = imglink.extract_product_page(html)
    assert info["brand"] == "Express", info
    assert info["sizes"] == "0,2,4", info


def test_ai_fill_parse():
    """parse_ai_fill coerces raw model output into clean fields (no Ollama)."""
    from app import aifill

    # moondream-style labeled lines (default format)
    got = aifill.parse_ai_fill("NAME: Navy Tee\nBRAND: Old Navy\nCOLOR: navy\n"
                               "CATEGORY: shirt\nSIZES: S, M, L\n")
    assert got["name"] == "Navy Tee" and got["brand"] == "Old Navy"
    assert got["color"] == "navy" and got["category"] == "top"  # shirt → top
    assert got["sizes"] == "S,M,L", got

    # labeled lines tolerate leading prose + blank values
    got = aifill.parse_ai_fill("Here is what I see:\nNAME: Jeans\nBRAND:\n"
                               "COLOR: blue\nCATEGORY: pants\nSIZES: 28, 30, 32\n")
    assert got["name"] == "Jeans" and got["brand"] == "" and got["category"] == "bottom"
    assert got["sizes"] == "28,30,32", got

    # filler words ("Not visible" / "Blank" / "No visible brand name") are empty
    got = aifill.parse_ai_fill("NAME: Jeans\nBRAND: Not visible\nCOLOR: blue\n"
                               "CATEGORY: bottom\nSIZES: Blank\n")
    assert got["brand"] == "" and got["sizes"] == "", got
    got = aifill.parse_ai_fill("NAME: Top\nBRAND: No visible brand name\nCOLOR: navy\n"
                               "CATEGORY: top\nSIZES: Not visible\n")
    assert got["brand"] == "" and got["sizes"] == "", got

    # JSON straight from the model (llava/qwen style) still parses
    got = aifill.parse_ai_fill('{"name":"Navy Tee","brand":"Old Navy","color":"navy",'
                               '"category":"shirt","sizes":"S, M, L"}')
    assert got["name"] == "Navy Tee" and got["brand"] == "Old Navy"
    assert got["category"] == "top" and got["sizes"] == "S,M,L", got

    # markdown code fence + extra prose tolerated
    got = aifill.parse_ai_fill('```json\n{"name":"Jeans","color":"blue",'
                               '"category":"pants","sizes":["28","30","32"]}\n```')
    assert got["category"] == "bottom" and got["sizes"] == "28,30,32", got

    # unknown category → dropped; no data → None
    got = aifill.parse_ai_fill('{"name":"X","category":"mystery","sizes":""}')
    assert got["category"] == "" and got["sizes"] == ""
    assert aifill.parse_ai_fill("no json here") is None
    assert aifill.parse_ai_fill("") is None


def test_aifill_llamacpp_vision_path():
    """VISION_ENGINE=llamacpp posts to the OpenAI-compatible /v1/chat/completions
    (image as a data URI) and parses the reply — the homelab-standard backend."""
    from unittest import mock
    from app import aifill

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": (
                "NAME: Navy Tee\nBRAND: Old Navy\nCOLOR: navy\n"
                "CATEGORY: top\nSIZES: S, M, L")}}]}

    old_engine, old_url = aifill.settings.vision_engine, aifill.settings.vision_url
    aifill.settings.vision_engine = "llamacpp"
    aifill.settings.vision_url = "http://vision:1234"
    try:
        with mock.patch.object(aifill.httpx, "post", return_value=FakeResp()) as post:
            got = aifill.ai_fill_garment(b"\x89PNG fake")
    finally:
        aifill.settings.vision_engine, aifill.settings.vision_url = old_engine, old_url

    assert got and got["name"] == "Navy Tee" and got["brand"] == "Old Navy"
    assert got["color"] == "navy" and got["category"] == "top" and got["sizes"] == "S,M,L"
    url = post.call_args[0][0]
    payload = post.call_args.kwargs["json"]
    assert url == "http://vision:1234/v1/chat/completions", url
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_phash_similarity():
    """dHash: identical → same hash, near-identical → tiny distance, different
    garments → large distance."""
    from app import phash
    from PIL import Image, ImageDraw
    import io as _io

    def make(color, label):
        img = Image.new("RGB", (240, 240), color)
        d = ImageDraw.Draw(img)
        d.rectangle([40, 40, 200, 200], fill=tuple(max(0, c - 40) for c in color))
        d.text((60, 90), label, fill=(255, 255, 255))
        buf = _io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    a = make((100, 60, 60), "AA")
    a_same = make((100, 60, 60), "AA")
    a_close = make((110, 66, 66), "AA")
    b = make((60, 100, 200), "ZZ")
    assert phash.image_phash(a) == phash.image_phash(a_same)
    assert phash.hamming(phash.image_phash(a), phash.image_phash(a_close)) <= 4
    assert phash.hamming(phash.image_phash(a), phash.image_phash(b)) >= 15
    assert phash.hamming("0" * 16, "0" * 16) == 0


def test_near_duplicates():
    """media.near_duplicates flags the same/near-identical item (even across
    categories — the same picture is the same item no matter how it's tagged),
    excludes self, and ignores unrelated garments (different color)."""
    from app import media, phash
    from PIL import Image, ImageDraw
    import io as _io

    def make(color, label):
        img = Image.new("RGB", (240, 240), color)
        d = ImageDraw.Draw(img)
        d.rectangle([40, 40, 200, 200], fill=color)
        d.text((60, 90), label, fill=(0, 0, 0))
        buf = _io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    ua = auth.create_user("wDup@example.com", "password123")
    g1 = w.create(ua["id"], "Red dress", "dress")
    media.save_garment_image(ua["id"], g1.id, make((120, 50, 50), "RED"), "png")
    g2 = w.create(ua["id"], "Blue dress", "dress")
    media.save_garment_image(ua["id"], g2.id, make((60, 60, 140), "BLU"), "png")
    # g3 = the same photo under a different category (a top vs the red dress).
    # A near-identical image is the same item regardless of how it's tagged, so
    # this IS a cross-category duplicate the user wants flagged.
    g3 = w.create(ua["id"], "Red top", "top")
    media.save_garment_image(ua["id"], g3.id, make((126, 54, 54), "RED"), "png")

    red1 = make((120, 50, 50), "RED")
    red2 = make((126, 54, 54), "RED")
    # g2 is unrelated → no near-dup for g1's exact hash (excluding g1 itself)
    assert media.near_duplicates(ua["id"], phash.image_phash(red1),
                                 phash.image_color_class(red1), exclude_id=g1.id) == []
    # a near-identical re-shoot of g1 flags g1 (same category + same color)
    dups = media.near_duplicates(ua["id"], phash.image_phash(red2),
                                 phash.image_color_class(red2))
    assert any(x["id"] == g1.id for x in dups), dups
    assert not any(x["id"] == g2.id for x in dups), dups  # different color class
    # g3 is the same picture tagged as a different category — a near-identical
    # image bypasses the category gate (cross-category duplicate)
    assert any(x["id"] == g3.id for x in dups), dups
    # a blue near-twin of g2 flags g2 (color gate passes, not g1)
    blue2 = make((66, 66, 146), "BLU")
    dups2 = media.near_duplicates(ua["id"], phash.image_phash(blue2),
                                  phash.image_color_class(blue2))
    assert any(x["id"] == g2.id for x in dups2), dups2
    assert not any(x["id"] == g1.id for x in dups2), dups2
    # garment_dict exposes near_dup_of for the grid "similar to" note — now
    # cross-category + debate zone, so g1 (red dress) notes g3 (red top, same pic)
    g1d = media.garment_dict(ua["id"], w.get(ua["id"], g1.id))
    assert g1d["phash"] != ""
    assert g1d["near_dup_of"] is not None and g1d["near_dup_of"]["id"] == g3.id
    assert media.garment_dict(ua["id"], w.get(ua["id"], g2.id))["near_dup_of"] is None


def test_near_duplicates_canonical_color_gate():
    """A red garment is never 'similar to' a pink one, even when their photos
    give the same coarse color class — the canonical color tags gate the match
    (the user's exact complaint: red one-piece ~ pink polka-dot swimsuit)."""
    from app import media, phash
    from PIL import Image, ImageDraw
    import io as _io

    def make(color):
        img = Image.new("RGB", (240, 240), color)
        ImageDraw.Draw(img).rectangle([40, 40, 200, 200], fill=color)
        buf = _io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    ua = auth.create_user("wDupC@example.com", "password123")
    gred = w.create(ua["id"], "One Piece Red", "dress",
                    color_hex="#a33333", color_tags="red")
    media.save_garment_image(ua["id"], gred.id, make((120, 50, 50)), "png")
    gpin = w.create(ua["id"], "Pink Polka Swim", "dress",
                    color_hex="#d9b3a0", color_tags="pink")
    media.save_garment_image(ua["id"], gpin.id, make((217, 179, 160)), "png")

    red = make((120, 50, 50))
    # same category; pink garment (canonical pink) must NOT match a red photo
    assert media.near_duplicates(ua["id"], phash.image_phash(red),
                                 phash.image_color_class(red),
                                 exclude_id=gred.id, color_tags="red") == []
    # a near-identical RED re-shoot still flags the red one (canonical red == red)
    red2 = make((124, 52, 52))
    dups = media.near_duplicates(ua["id"], phash.image_phash(red2),
                                 phash.image_color_class(red2), color_tags="red")
    assert any(x["id"] == gred.id for x in dups), dups
    assert not any(x["id"] == gpin.id for x in dups), dups


def test_size_schemas():
    """size_schema drives type-aware size capture: pants = waist×length, bras =
    band×cup, shirts/dresses = size list, footwear = numeric."""
    from app.media import size_schema

    assert size_schema("bottom")["mode"] == "wxl"
    assert size_schema("bra")["mode"] == "bandcup"
    assert size_schema("top")["mode"] == "list"
    assert "XL" in size_schema("top")["options"]
    assert "9.5" in size_schema("footwear")["options"]
    assert size_schema("") == size_schema("top")  # unknown falls back to top


def test_normalize_orientation():
    """Garment photos are righted (EXIF) and forced to portrait for a
    consistent, upright display in the 3:4 grid."""
    from app.media import normalize_orientation
    from PIL import Image
    import io as _io

    def save(img, fmt="PNG", exif=None):
        buf = _io.BytesIO()
        img.save(buf, fmt, exif=exif)
        return buf.getvalue()

    # landscape (1200x800) → portrait (800x1200)
    data, ext = normalize_orientation(save(Image.new("RGB", (1200, 800), (120, 120, 120))))
    assert ext == "jpg"
    w, h = Image.open(_io.BytesIO(data)).size
    assert h > w, (w, h)

    # already-portrait image is left alone (no re-encode → ext stays '')
    data2, ext2 = normalize_orientation(save(Image.new("RGB", (800, 1200), (120, 120, 120))))
    assert ext2 == "", ext2

    # EXIF orientation 6 (stored landscape, meant portrait) is righted
    exif = Image.Exif()
    exif[0x0112] = 6
    data3, ext3 = normalize_orientation(save(Image.new("RGB", (1000, 500), (60, 60, 60)), "JPEG", exif=exif))
    assert ext3 == "jpg"
    w3, h3 = Image.open(_io.BytesIO(data3)).size
    assert h3 > w3

    # unreadable bytes → returned unchanged with ext ''
    assert normalize_orientation(b"not an image") == (b"not an image", "")


def test_normalize_orientation_rotations():
    """Orientation: 180 flips an upside-down garment in place (portrait); 90/270
    correct a sideways one (the tag reader chose it, so the frame may go
    horizontal); any other rotation on a landscape frame is forced to portrait."""
    from app.media import normalize_orientation
    from PIL import Image
    import io as _io

    def data_of(img):
        buf = _io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def size(d):
        return Image.open(_io.BytesIO(d)).size

    land = Image.new("RGB", (1200, 800), (120, 120, 120))
    port = Image.new("RGB", (800, 1200), (120, 120, 120))

    # 180 flip on a portrait → stays portrait (flipped in place)
    d, e = normalize_orientation(data_of(port), rotate=180)
    assert size(d) == (800, 1200), size(d)

    # 90/270 on a portrait → sideways garment corrected (frame goes landscape)
    for r, out in ((90, (1200, 800)), (270, (1200, 800))):
        d, e = normalize_orientation(data_of(port), rotate=r)
        assert size(d) == out, (r, size(d))

    # landscape + portrait-preserving/no/invalid rotation → forced to portrait
    for r in (0, 180, 45):
        d, e = normalize_orientation(data_of(land), rotate=r)
        w, h = size(d)
        assert h > w, (r, w, h)

    # landscape + a deliberate 90/270 → rotation applied (no portrait fallback
    # double-rotation) — a landscape frame rotated 90/270 becomes portrait
    for r in (90, 270):
        d, e = normalize_orientation(data_of(land), rotate=r)
        assert size(d) == (800, 1200), (r, size(d))


def test_normalize_color():
    """normalize_color maps free-text variants onto the canonical palette so a
    color is only ever stored one way (no 'navy blue' vs 'navy' mismatches)."""
    from app.media import normalize_color

    assert normalize_color("") == ""
    assert normalize_color("navy") == "navy"
    assert normalize_color("Navy Blue") == "navy"
    assert normalize_color("dark navy") == "navy"
    assert normalize_color("olive green") == "olive"
    assert normalize_color("grey") == "gray"
    assert normalize_color("charcoal") == "gray"
    assert normalize_color("black&white") == "black"
    assert normalize_color("black and white") == "black"
    assert normalize_color("forest green") == "green"
    # the blue family is split into shades so a light-wash jean ≠ a navy one
    assert normalize_color("light blue") == "light blue"
    assert normalize_color("sky blue") == "light blue"
    assert normalize_color("baby blue") == "light blue"
    assert normalize_color("powder blue") == "light blue"
    assert normalize_color("indigo") == "indigo"
    assert normalize_color("denim blue") == "indigo"
    assert normalize_color("denim") == "indigo"
    assert normalize_color("royal blue") == "blue"
    assert normalize_color("dark blue") == "blue"
    assert normalize_color("unknowncolor") == "unknowncolor"  # kept as-is (still an option)


def test_detect_color_and_refine():
    """detect_color re-derives a specific shade from the photo's pixels;
    refine_color only refines a coarse color within the same family and never
    overrides a specific tag color (navy/black/white)."""
    from app.media import detect_color, refine_color
    from PIL import Image
    import io as _io

    def png(rgb):
        buf = _io.BytesIO()
        Image.new("RGB", (300, 400), rgb).save(buf, "PNG")
        return buf.getvalue()

    # neutral fallback by overall lightness
    assert detect_color(png((25, 25, 28))) == "black"
    assert detect_color(png((120, 120, 120))) == "gray"
    assert detect_color(png((245, 245, 245))) == "white"
    # blue family shades (the split that matters most)
    assert detect_color(png((40, 100, 200))) == "blue"         # bright / royal
    assert detect_color(png((180, 200, 230))) == "light blue"  # pale / sky
    assert detect_color(png((140, 170, 210))) == "light blue"  # light-wash denim
    assert detect_color(png((40, 50, 95))) == "indigo"         # dark-wash denim
    assert detect_color(png((25, 32, 60))) == "navy"
    # other families
    assert detect_color(png((180, 60, 60))) == "red"
    assert detect_color(png((110, 120, 60))) == "olive"
    # refine: coarse blue → specific shade, same family only
    assert refine_color("blue", png((40, 50, 95))) == "indigo"
    assert refine_color("blue", png((140, 170, 210))) == "light blue"
    assert refine_color("blue", png((180, 60, 60))) == "blue"  # red can't refine blue
    # specific tag colors are never overridden by the photo
    assert refine_color("navy", png((180, 200, 230))) == "navy"
    assert refine_color("black", png((40, 50, 95))) == "black"
    assert refine_color("white", png((180, 200, 230))) == "white"
    assert refine_color("", png((180, 60, 60))) == ""


def test_color_class():
    """image_color_class buckets dominant color coarsely (drives the dup gate)."""
    from app import phash
    from PIL import Image
    import io as _io

    def make(color):
        buf = _io.BytesIO()
        Image.new("RGB", (64, 64), color).save(buf, "PNG")
        return buf.getvalue()

    assert phash.image_color_class(make((40, 40, 40))) == "black"
    assert phash.image_color_class(make((235, 235, 235))) == "white"
    assert phash.image_color_class(make((128, 128, 128))) == "gray"
    assert phash.image_color_class(make((220, 40, 40))) == "red"
    assert phash.image_color_class(make((10, 90, 40))) == "green"
    assert phash.image_color_class(make((40, 60, 200))) == "blue"
    assert phash.image_color_class(b"not an image") == ""


def test_extract_product_page_color_from_text():
    html = """<html><head>
      <meta property="og:title" content="Navy Oxford Shirt - Gap" />
      <meta property="og:image" content="https://shop.com/logo.png" />
    </head><body><img src="https://shop.com/alt.png" alt="Image number 1 showing, Navy Oxford Shirt" /></body></html>"""
    info = imglink.extract_product_page(html)
    assert info["name"] == "Navy Oxford Shirt", info
    assert info["color"] == "navy", info
    assert info["category"] == "top", info


def test_extract_product_page_expanded_images():
    # gallery shots + flat-lay/outfit shots included; TP=NAV nav shots excluded
    html = """<html><body>
      <img src="https://cdn.com/img1.png?width=737" alt="Image number 1 showing, Pants" />
      <img src="https://cdn.com/img2.png?width=737" alt="Image number 2 showing, Pants" />
      <img src="https://cdn.com/nav-shot.jpg?TP=NAV" alt="Image of a model wearing the collection" />
      <img src="https://cdn.com/flat-lay.jpg" alt="" />
    </body></html>"""
    urls = imglink.extract_product_page(html)["images"]
    assert len(urls) == 3, urls  # 2 gallery + 1 flat-lay
    assert "nav-shot.jpg" not in " ".join(urls)
    assert "flat-lay.jpg" in " ".join(urls)


def test_clean_image_url():
    # tiny thumbnail width rewritten up; tracking/junk params dropped
    got = imglink.clean_image_url(
        "https://oldnavy.gap.com/webcontent/0023/543/365/cn23343565.jpg"
        "?width=92&ts=1700000000&srsltid=afjkl&utm_source=newsletter"
    )
    assert "width=1024" in got, got
    assert "ts=" not in got
    assert "srsltid" not in got
    assert "utm_source" not in got
    # wid / w variants handled too
    assert "wid=1024" in imglink.clean_image_url("https://cdn.com/x.jpg?wid=60")
    assert "w=1024" in imglink.clean_image_url("https://cdn.com/x.jpg?w=200")
    # already-large widths are left alone
    assert "width=1600" in imglink.clean_image_url("https://cdn.com/x.jpg?width=1600&qlt=80")
    # non-width params preserved
    got = imglink.clean_image_url("https://cdn.com/x.jpg?width=92&qlt=70&fmt=webp&op_sharpen=1")
    assert "qlt=70" in got and "fmt=webp" in got and "op_sharpen=1" in got
    # no-query / data / protocol-relative pass through sensibly
    assert imglink.clean_image_url("https://cdn.com/x.jpg") == "https://cdn.com/x.jpg"
    assert imglink.clean_image_url("data:image/png;base64,AAAA") == "data:image/png;base64,AAAA"
    assert "width=1024" in imglink.clean_image_url("//cdn.com/x.jpg?width=92")


if __name__ == "__main__":
    test_create_upload_serve_delete()
    test_cross_user_isolation()
    test_rotate_180_flips_and_stays_portrait()
    test_update()
    test_imglink_product_gallery_preferred_over_logo()
    test_imglink_jsonld_product_image()
    test_imglink_og_image_last_resort_and_byte_detection()
    test_extract_product_page()
    test_extract_product_page_color_from_text()
    test_extract_product_page_expanded_images()
    test_clean_image_url()
    test_normalize_orientation_rotations()
    test_near_duplicates_canonical_color_gate()
    print("wardrobe tests OK")
