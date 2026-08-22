"""Shared store singletons.

Every store wraps the same sqlite connection (see db.py), so creating them
once here and importing them from routers avoids redundant instances and keeps
a single well-known source for the data layer.
"""
from __future__ import annotations

from .clips import ClipStore
from .outfits import OutfitStore
from .wardrobe import Wardrobe

wardrobe = Wardrobe()
outfits = OutfitStore()
clips = ClipStore()
