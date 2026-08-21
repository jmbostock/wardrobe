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


def test_cross_user_isolation():
    ua = auth.create_user("wA@example.com", "password123")
    ub = auth.create_user("wB@example.com", "password123")
    g = w.create(ua["id"], "Mine", "top")
    assert w.get(ub["id"], g.id) is None
    assert w.update_image(ub["id"], g.id, "x.png") is False
    assert w.delete(ub["id"], g.id) is False
    assert w.get(ua["id"], g.id) is not None


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
       "color":"Blue","image":["//cdn.shop.com/pants-front.png","//cdn.shop.com/pants-back.png"]}
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
    # JSON-LD images first, then gallery alt images, then product-ish; no logo
    assert "//cdn.shop.com/pants-front.png" in info["images"]
    assert "//cdn.shop.com/pants-back.png" in info["images"]
    assert "logo" not in " ".join(info["images"]).lower()


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
    test_imglink_product_gallery_preferred_over_logo()
    test_imglink_jsonld_product_image()
    test_imglink_og_image_last_resort_and_byte_detection()
    test_extract_product_page()
    test_extract_product_page_color_from_text()
    test_extract_product_page_expanded_images()
    test_clean_image_url()
    print("wardrobe tests OK")
