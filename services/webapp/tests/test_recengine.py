"""Rec-engine Layer 2/3 tests — embeddings, style vector, ALS scoring, fusion.

All numpy/fake-data — no FashionCLIP/torch/implicit required (those run in
scripts/rec_build.py, the ML venv). Verifies the request-time path: embeddings
persist, the style centroid is a weighted mean of engaged garments, the ALS
model loads + scores from an npz, and Personalizer stays inert until there's
real data (so the rule recommender is untouched for cold users). Runnable
without pytest:
    python services/webapp/tests/test_recengine.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-recengine-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, embeddings, interactions, learner, personalize, recommender  # noqa: E402
from app.main import app  # noqa: E402
from app.store import wardrobe  # noqa: E402

DIM = 8  # small vectors for tests (real model is 512-d)

_C = TestClient(app)


def _mkuser(email: str) -> int:
    try:
        auth.create_user(email, "password123")
    except auth.AuthError:
        pass
    tok = _C.post(
        "/api/auth/login", json={"email": email, "password": "password123"}
    ).json()["token"]
    return auth.get_user_by_token(tok)["id"]


def _embed(gid: int, vec: np.ndarray) -> None:
    embeddings.save_embedding(gid, vec, model="test")


def _seed_style_user(user_id: int) -> list[int]:
    """Create 3 tops, embed them near a shared 'style axis', and log strong
    engagement (worn) so a style centroid exists."""
    gids = []
    for i, axis in enumerate(([1, 0, 0, 0, 0, 0, 0, 0],
                              [0.9, 0.1, 0, 0, 0, 0, 0, 0],
                              [0.8, 0.2, 0, 0, 0, 0, 0, 0])):
        g = wardrobe.create(user_id, f"Style top {i}", "top", color_tags="navy")
        gids.append(g.id)
        _embed(g.id, np.asarray(axis, dtype=float))
    for gid in gids:
        interactions.log(user_id, gid, "worn", {"test": True})
    return gids


def test_embedding_roundtrip_and_cosine():
    vec = np.random.rand(DIM).astype(np.float32)
    embeddings.save_embedding(999_001, vec, model="test")
    got = embeddings.get_vector(999_001)
    assert got is not None and np.allclose(got, vec, atol=1e-6)
    assert embeddings.get_vector(999_999) is None
    assert abs(embeddings.cosine(vec, vec) - 1.0) < 1e-6
    orth = -vec.copy()
    assert abs(embeddings.cosine(vec, orth) + 1.0) < 1e-6  # opposite → -1


def test_style_vector_needs_data():
    uid = _mkuser("cold@example.com")
    assert embeddings.user_style_vector(uid) is None  # no interactions yet


def test_style_vector_centroid():
    uid = _mkuser("style@example.com")
    _seed_style_user(uid)
    v = embeddings.user_style_vector(uid)
    assert v is not None
    # centroid should point mostly along the shared 'style axis' (+x)
    assert abs(v[0]) > 0.8, f"expected x-dominant centroid, got {v}"


def test_learner_load_and_score():
    out = Path(os.environ["DATA_DIR"]) / "rec"
    out.mkdir(parents=True, exist_ok=True)
    uids = np.asarray([1, 2], dtype=np.int64)
    iids = np.asarray([10, 20], dtype=np.int64)
    uf = np.random.rand(2, 4)
    itf = np.random.rand(2, 4)
    np.savez(out / "als.npz", user_ids=uids, item_ids=iids,
             user_factors=uf, item_factors=itf)
    model = learner.load_model()
    assert model is not None
    expect = float(np.dot(uf[0], itf[0]))
    assert abs(learner.als_score(1, 10, model) - expect) < 1e-6
    assert learner.als_score(1, 99, model) is None  # unknown item
    assert learner.als_score(99, 10, model) is None  # unknown user


def test_personalizer_inactive_without_data():
    uid = _mkuser("nobody@example.com")
    g = wardrobe.create(uid, "Lonely tee", "top", color_tags="white")
    _embed(g.id, np.zeros(DIM, dtype=float))
    p = personalize.Personalizer(uid, [wardrobe.get(uid, g.id)])
    assert p.active is False
    assert p.bonus(g.id) == (0.0, 0.0)


def test_personalizer_active_with_style_data():
    uid = _mkuser("warm@example.com")
    gids = _seed_style_user(uid)
    items = [wardrobe.get(uid, gid) for gid in gids]
    p = personalize.Personalizer(uid, items)
    assert p.active is True
    assert p.alpha > 0 and p.gamma == 0  # style on, ALS off (no model)
    st, al = p.bonus(gids[0])
    assert 0.0 <= st <= 1.0 and al == 0.0


def test_recommender_fusion_does_not_break():
    uid = _mkuser("fusion@example.com")
    gids = _seed_style_user(uid)
    items = [wardrobe.get(uid, gid) for gid in gids]
    p = personalize.Personalizer(uid, items)
    # with the style bonus active, the highest-scoring top should get a boost
    scored = [(p.add_to_score(g.id, 50.0), g.id) for g in items]
    assert scored[0][0] >= 50.0  # never lowers a score
    # recommend() still returns a valid outfit with personalization noted
    res = recommender.recommend(
        recommender.Weather(temp_c=22.0, condition="clear"), "casual",
        wardrobe=wardrobe, user_id=uid,
    )
    assert res["outfit"].get("top") is not None
    assert any("personalized" in line for line in res["reasoning"])


def test_rec_build_matrix_and_save():
    """scripts/rec_build.py's matrix builder + npz writer must round-trip through
    app.learner (scipy-only — implicit is only needed for the fit itself)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rec_build",
        str(Path(__file__).resolve().parents[3] / "scripts" / "rec_build.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rows = [(1, 101, 4.0), (1, 102, 3.0), (2, 101, 2.0), (2, 102, -2.0)]
    matrix, users, items = mod.build_matrix(rows)
    assert users == [1, 2] and items == [101, 102]
    assert matrix.shape == (2, 2)
    assert matrix[0, 0] == 4.0  # user 1 × item 101 confidence

    out = mod.save_model(
        Path(os.environ["DATA_DIR"]) / "rec", users, items,
        np.random.rand(2, 4), np.random.rand(2, 4),
    )
    assert out.is_file()
    model = learner.load_model()
    assert model is not None
    assert learner.als_score(1, 101, model) is not None
    assert learner.als_score(9, 101, model) is None


def _main() -> None:
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
