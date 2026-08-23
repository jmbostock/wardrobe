"""Personalization fusion — adds ML signals to the rule score (rec-engine L2/3).

Built once per `recommend()` call, `Personalizer` precomputes two bonus terms for
every candidate garment:

  - style: cosine of the garment embedding with the user's style centroid
    (embeddings.user_style_vector) — present once the user has ≥3 engaged garments.
  - als:   normalized dot-product from the ALS collaborative model — present only
    when the model exists AND the user has ≥ MIN_INTERACTIONS.

Both are normalized to [0,1] and scaled by β/γ onto the rule-score scale (which
peaks around 40–100). With no data the personalizer is inactive and the output is
identical to the pure rule-based recommender.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from . import embeddings, learner

STYLE_BETA = 0.30   # weight of the style-similarity term (rules scale, 0..~40)
ALS_GAMMA = 0.20    # weight of the ALS term (rules scale, 0..~40)
SCALE = 40.0        # rules' warmth term is 40 — use it as the normalization scale


class Personalizer:
    """Precomputed per-garment {style, als} bonus map for one user + candidate set."""

    def __init__(self, user_id: int, items: list[Any] | None = None) -> None:
        self.user_id = user_id
        self.alpha = 0.0   # active style weight
        self.gamma = 0.0   # active ALS weight
        self._bonus: dict[int, list[float]] = {}
        items = items or []

        # ---- style similarity (Layer 2) ----
        style = embeddings.user_style_vector(user_id)
        if style is not None:
            self.alpha = STYLE_BETA
            for g in items:
                vec = embeddings.get_vector(g.id)
                if vec is not None:
                    # map cosine [-1,1] → [0,1]
                    self._bonus[g.id] = [
                        max(0.0, (embeddings.cosine(style, vec) + 1.0) / 2.0),
                        0.0,
                    ]

        # ---- collaborative learning (Layer 3) ----
        model = learner.load_model()
        if (
            model is not None
            and learner.interaction_count(user_id) >= learner.MIN_INTERACTIONS
        ):
            self.gamma = ALS_GAMMA
            for g in items:
                s = learner.als_score(user_id, g.id, model)
                if s is not None:
                    row = self._bonus.get(g.id, [0.0, 0.0])
                    row[1] = s
                    self._bonus[g.id] = row
            # min-max normalize ALS scores to [0,1] across the candidate set
            vals = [v[1] for v in self._bonus.values()]
            if vals:
                lo, hi = min(vals), max(vals)
                if hi > lo:
                    for k in self._bonus:
                        self._bonus[k][1] = (self._bonus[k][1] - lo) / (hi - lo)
                else:
                    for k in self._bonus:
                        self._bonus[k][1] = 0.5

    @property
    def active(self) -> bool:
        return bool(self._bonus)

    def bonus(self, garment_id: int) -> tuple[float, float]:
        """(style, als) bonuses for a garment, each in [0,1] (defaults 0)."""
        b = self._bonus.get(garment_id)
        return (b[0], b[1]) if b else (0.0, 0.0)

    def add_to_score(self, garment_id: int, score: float) -> float:
        """Add the scaled ML bonuses to a rule score."""
        if not self._bonus:
            return score
        st, al = self.bonus(garment_id)
        return score + SCALE * (self.alpha * st + self.gamma * al)
