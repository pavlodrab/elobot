"""Shared background-image helpers for /standings and /playoff PNGs.

Tournament owners can attach a custom background via /set_tournament_bg;
the bytes are stored in ``tournaments.bg_image_data`` (base64) so they
survive container redeploys. The legacy ``tournaments.bg_image_path``
column is still consulted as a fallback when the DB blob is absent.
When neither is available callers fall back to their own flat-colour
``Image.new``.
"""
from __future__ import annotations

import base64
import io
import logging
import os
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)


def make_canvas(
    width: int,
    height: int,
    *,
    bg_color: tuple[int, int, int],
    bg_image_path: Optional[str] = None,
    bg_image_data: Optional[str] = None,
    overlay_alpha: int = 165,
) -> Image.Image:
    """Return a fresh ``RGB`` canvas of the requested size.

    Source priority:
      1. ``bg_image_data`` — base64-encoded JPEG/PNG stored in the DB
         (survives redeploys).
      2. ``bg_image_path`` — path on local disk.
      3. ``bg_color`` — flat colour fallback.

    If a source resolves to a readable image, it is cover-fit onto the
    canvas (centered, cropping the longer dimension) and a translucent
    black overlay of ``overlay_alpha`` is painted on top so text stays
    readable.
    """
    src_img = _open_from_data(bg_image_data) or _open_from_path(bg_image_path)
    if src_img is not None:
        try:
            with src_img:
                src = src_img.convert("RGB")
                bg = _cover_resize(src, width, height)
                _apply_dark_overlay(bg, overlay_alpha)
                return bg
        except Exception as exc:
            log.warning(
                "make_canvas: failed to render bg (%s); falling back to flat colour",
                exc,
            )
    return Image.new("RGB", (width, height), bg_color)


def _open_from_data(b64: Optional[str]) -> Optional[Image.Image]:
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64, validate=False)
        return Image.open(io.BytesIO(raw))
    except Exception as exc:
        log.warning("make_canvas: bad bg_image_data (%s)", exc)
        return None


def _open_from_path(path: Optional[str]) -> Optional[Image.Image]:
    if not path:
        return None
    if not os.path.exists(path):
        log.info(
            "make_canvas: bg path %r missing on disk (post-redeploy?); "
            "falling back",
            path,
        )
        return None
    try:
        return Image.open(path)
    except Exception as exc:
        log.warning("make_canvas: failed to open bg %r (%s)", path, exc)
        return None


def _cover_resize(src: Image.Image, w: int, h: int) -> Image.Image:
    """CSS-``object-fit: cover`` clone of ``src`` at exactly ``w × h``."""
    sw, sh = src.size
    if sw == 0 or sh == 0:
        return Image.new("RGB", (w, h), (0, 0, 0))
    src_ratio = sw / sh
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        # Source is wider — match height, crop the sides.
        new_h = h
        new_w = max(w, int(round(new_h * src_ratio)))
    else:
        new_w = w
        new_h = max(h, int(round(new_w / src_ratio)))
    resized = src.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _apply_dark_overlay(img: Image.Image, alpha: int) -> None:
    """Paint a translucent black overlay over ``img`` in-place."""
    if alpha <= 0:
        return
    overlay = Image.new("RGBA", img.size, (0, 0, 0, max(0, min(alpha, 255))))
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))
