"""Mixed text + color-emoji rendering for the bot's PNG outputs.

Pillow can rasterise color emoji from a CBDT/COLR font (NotoColorEmoji
ships as bitmap CBDT) when given ``embedded_color=True``, but it can't
mix a regular text font and a color-emoji font in a single
``draw.text`` call. This module bridges that gap:

* :func:`split_emoji_runs` segments a string into alternating
  ``(text, is_emoji)`` runs based on Unicode emoji ranges.
* :func:`draw_text_with_emoji` draws each run with the appropriate
  font onto a Pillow canvas, returning the new x-cursor.
* :func:`measure_text_with_emoji` reports the rendered width so
  layout code (right-justified scores, centred headers, etc.) can
  account for the resized emoji glyphs.

NotoColorEmoji is a bitmap font with a single native size (109 px),
so we render the emoji into a private RGBA canvas at native size,
trim the transparent margin, and resize via ``LANCZOS`` to match the
target line height. The result is cached per (codepoint, target_h)
so repeat lookups (the same 🏅 across 30 rows of standings) cost
nothing after the first render.

If the emoji font is missing or Pillow throws, the helper transparently
falls back to drawing the emoji text with the regular font — the worst
case is the legacy "tofu / square" rendering, never a crash.
"""
from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


_HERE = os.path.dirname(os.path.abspath(__file__))
EMOJI_FONT_PATH = os.path.join(_HERE, "assets", "NotoColorEmoji.ttf")
NATIVE_EMOJI_SIZE = 109  # NotoColorEmoji ships as a 109 px bitmap font.


# Conservative Unicode ranges covering every grapheme that should be
# rendered through the color-emoji font instead of the regular text
# font. Includes pictographic ranges, regional indicators (flags),
# enclosed alphanumerics for ⓘ/Ⓜ-style icons, and the common dingbats.
#
# Country flags (🇺🇦, 🇩🇪, …) are encoded as a *pair* of regional-
# indicator codepoints (U+1F1E6-U+1F1FF). NotoColorEmoji only renders
# the combined flag glyph when the two indicators arrive together —
# in isolation each indicator is drawn as a letter in a square box.
# So we must match the pair as a single grapheme, never one at a time.
#
# Subdivision flags (🏴󠁧󠁢󠁥󠁮󠁧󠁿 England, 🏴󠁧󠁢󠁳󠁣󠁴󠁿 Scotland, 🏴󠁧󠁢󠁷󠁬󠁳󠁿 Wales)
# are encoded as a black flag (U+1F3F4) followed by an ISO-3166-2-style
# tag sequence in the U+E0020-U+E007E range, terminated by U+E007F
# (CANCEL TAG). The tag chars are invisible in regular text fonts, so
# we MUST capture the whole sequence as one grapheme — otherwise the
# 🏴 leaks into the emoji branch alone and renders as a plain black
# flag, with the tag chars dropped into the regular-text run.
_EMOJI_RE = re.compile(
    "(?:"
    # Subdivision flag: black flag + tag sequence + cancel tag.
    # Must come before the generic 🏴 match in the emoji range below.
    "\U0001F3F4[\U000E0020-\U000E007E]+\U000E007F"
    "|"
    # Country flag: exactly two consecutive regional indicators.
    "[\U0001F1E6-\U0001F1FF]{2}"
    "|"
    # Single emoji codepoint from one of the supported ranges,
    # optionally followed by VS-16 / ZWJ-joined continuations
    # (handles 👨\u200d👩\u200d👧 family sequences, ❤\ufe0f, …).
    "["
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001F77F"   # alchemical
    "\U0001F780-\U0001F7FF"   # geometric shapes ext
    "\U0001F800-\U0001F8FF"   # supplemental arrows-c
    "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"   # chess symbols
    "\U0001FA70-\U0001FAFF"   # symbols & pictographs ext-a
    "\u2300-\u23FF"           # misc technical (⏰ etc.)
    "\u2600-\u26FF"           # misc symbols (☀ ☔ etc.)
    "\u2700-\u27BF"           # dingbats (✨ ❤ etc.)
    "\u2B00-\u2BFF"           # misc symbols & arrows
    "\u3030\u303D\u3297\u3299"
    "]"
    "(?:\ufe0f|\u200d.)*"
    ")",
    flags=re.UNICODE,
)


def _emoji_font_available() -> bool:
    return os.path.exists(EMOJI_FONT_PATH)


@lru_cache(maxsize=1)
def _native_emoji_font() -> Optional[ImageFont.FreeTypeFont]:
    """Load NotoColorEmoji at its native 109 px once per process."""
    if not _emoji_font_available():
        return None
    try:
        return ImageFont.truetype(EMOJI_FONT_PATH, NATIVE_EMOJI_SIZE)
    except Exception as e:
        log.warning("emoji_helper: could not load %s: %s", EMOJI_FONT_PATH, e)
        return None


@lru_cache(maxsize=1)
def _raqm_available() -> bool:
    """Check whether Pillow has HarfBuzz/Raqm shaping (libraqm) at runtime.

    Subdivision-flag emojis (🏴󠁧󠁢󠁥󠁮󠁧󠁿 / 🏴󠁧󠁢󠁳󠁣󠁴󠁿 / 🏴󠁧󠁢󠁷󠁬󠁳󠁿) are encoded as
    a black flag plus an ISO-3166-2 tag sequence and only render as the
    correct flag when Pillow can apply OpenType GSUB substitutions via
    libraqm. Without raqm the tag chars are dropped and only a plain
    black flag remains. The bot deploys (Dockerfile / nixpacks.toml)
    install ``libraqm0`` + ``libfribidi0`` so Pillow's _imagingft can
    dlopen them at runtime.
    """
    try:
        from PIL import features  # type: ignore
        return bool(features.check("raqm"))
    except Exception:
        return False


_NO_RAQM_WARNED = False


def _warn_no_raqm_once() -> None:
    """Log a single warning when a subdivision flag is rendered without
    libraqm — the result will be a plain black flag instead of the
    intended England / Scotland / Wales glyph."""
    global _NO_RAQM_WARNED
    if _NO_RAQM_WARNED:
        return
    _NO_RAQM_WARNED = True
    log.warning(
        "emoji_helper: libraqm is not available; subdivision-flag emojis "
        "(England / Scotland / Wales) will render as a plain black flag. "
        "Install libraqm0 + libfribidi0 in the deploy image."
    )


def split_emoji_runs(text: str) -> list[Tuple[str, bool]]:
    """Split ``text`` into runs of ``(piece, is_emoji)``.

    Adjacent same-kind runs are emitted separately when the regex
    fires multiple times, but consumers can treat that as a single
    run safely.

    Empty strings produce an empty list.
    """
    if not text:
        return []
    runs: list[Tuple[str, bool]] = []
    last_end = 0
    for m in _EMOJI_RE.finditer(text):
        start, end = m.span()
        if start > last_end:
            runs.append((text[last_end:start], False))
        runs.append((text[start:end], True))
        last_end = end
    if last_end < len(text):
        runs.append((text[last_end:], False))
    return runs


@lru_cache(maxsize=512)
def _render_emoji_glyph_png(grapheme: str, target_h: int) -> Optional[bytes]:
    """Rasterise a single emoji grapheme into a target-height PNG.

    Cached per (grapheme, height) to avoid re-rendering the same icon
    on every row of every PNG. Returns the PNG bytes or ``None`` when
    the rendering fails (caller falls back to regular text).
    """
    ef = _native_emoji_font()
    if ef is None:
        return None
    # Subdivision flag (England / Scotland / Wales): 🏴 + tag sequence
    # + cancel-tag. Renders correctly only when Pillow has libraqm at
    # runtime; warn once so the missing-shaper case is obvious in logs.
    if (
        len(grapheme) >= 3
        and grapheme[0] == "\U0001F3F4"
        and grapheme[-1] == "\U000E007F"
        and not _raqm_available()
    ):
        _warn_no_raqm_once()
    try:
        canvas = Image.new(
            "RGBA",
            (NATIVE_EMOJI_SIZE * 2, NATIVE_EMOJI_SIZE * 2),
            (0, 0, 0, 0),
        )
        d = ImageDraw.Draw(canvas)
        d.text((0, 0), grapheme, font=ef, embedded_color=True)
        bbox = canvas.getbbox()
        if not bbox:
            return None
        cropped = canvas.crop(bbox)
        cw, ch = cropped.size
        if ch == 0:
            return None
        scale = target_h / ch
        new_w = max(1, int(round(cw * scale)))
        new_h = max(1, int(round(ch * scale)))
        try:
            resampler = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
        except AttributeError:
            resampler = Image.LANCZOS  # type: ignore[attr-defined]
        scaled = cropped.resize((new_w, new_h), resampler)
        import io
        buf = io.BytesIO()
        scaled.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        log.warning(
            "emoji_helper: render of %r at %d failed: %s",
            grapheme, target_h, e,
        )
        return None


def _emoji_image(grapheme: str, target_h: int) -> Optional[Image.Image]:
    """Return a Pillow image for the rendered glyph (or None on failure)."""
    raw = _render_emoji_glyph_png(grapheme, target_h)
    if not raw:
        return None
    import io
    try:
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None


def _approx_line_height(font: ImageFont.FreeTypeFont) -> int:
    """Best-effort line height for a regular text font — used to size
    emojis so they share the cap-height of the surrounding text."""
    try:
        ascent, descent = font.getmetrics()
        return max(1, int(ascent + descent))
    except Exception:
        try:
            return max(1, int(font.size))
        except Exception:
            return 16


def measure_text_with_emoji(
    text: str,
    font: ImageFont.FreeTypeFont,
) -> int:
    """Width in pixels of ``text`` rendered through
    :func:`draw_text_with_emoji` — the sum of regular-font widths and
    scaled emoji-glyph widths.
    """
    if not text:
        return 0
    if not _emoji_font_available():
        try:
            return int(font.getlength(text))
        except Exception:
            return int(font.getbbox(text)[2])
    target_h = _approx_line_height(font)
    total = 0
    for piece, is_emoji in split_emoji_runs(text):
        if is_emoji:
            img = _emoji_image(piece, target_h)
            if img is not None:
                total += img.width
                continue
        try:
            total += int(font.getlength(piece))
        except Exception:
            total += int(font.getbbox(piece)[2])
    return total


def draw_text_with_emoji(
    base: Image.Image,
    xy: Tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Tuple[int, int, int] | Tuple[int, int, int, int] = (255, 255, 255),
) -> int:
    """Draw ``text`` with mixed regular + color-emoji glyphs onto
    ``base`` (an RGBA Pillow image). Returns the new x-cursor (so
    callers can chain draws).

    * Emoji runs are rasterised via NotoColorEmoji and scaled to match
      the regular font's line height, then alpha-composited onto the
      canvas. Vertical alignment matches the text baseline.
    * Regular runs go through ``ImageDraw.text`` as before.
    * When the emoji font isn't available the function falls back to a
      single ``draw.text`` call with the regular font — emojis render
      as tofu but layout is preserved.
    """
    x, y = xy
    if not text:
        return x
    draw = ImageDraw.Draw(base)
    if not _emoji_font_available():
        draw.text((x, y), text, font=font, fill=fill)
        try:
            return x + int(font.getlength(text))
        except Exception:
            return x + int(font.getbbox(text)[2])

    target_h = _approx_line_height(font)
    # Approximate baseline by ascent so emoji glyphs sit on the same
    # line as text. Pillow draws text top-aligned by default; nudge
    # each emoji down by (ascent - emoji_h) // 2 so the icon is
    # visually centred against cap height.
    try:
        ascent, _descent = font.getmetrics()
    except Exception:
        ascent = target_h
    for piece, is_emoji in split_emoji_runs(text):
        if is_emoji:
            img = _emoji_image(piece, target_h)
            if img is not None:
                emoji_y = y + max(0, (ascent - img.height) // 2)
                if base.mode != "RGBA":
                    # alpha_composite needs RGBA; fall back to paste
                    # with the alpha channel as the mask.
                    base.paste(img, (int(x), int(emoji_y)), img)
                else:
                    base.alpha_composite(img, dest=(int(x), int(emoji_y)))
                x += img.width
                continue
        draw.text((x, y), piece, font=font, fill=fill)
        try:
            x += int(font.getlength(piece))
        except Exception:
            x += int(font.getbbox(piece)[2])
    return x


def truncate_text_with_emoji(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    *,
    suffix: str = "…",
) -> str:
    """Cut ``text`` so that ``measure_text_with_emoji(...)`` is at most
    ``max_width``. Adds ``suffix`` when truncation actually happens.

    Operates at run granularity first (don't slice through an emoji),
    then character-by-character on text runs. Used by standings PNG
    when a "nick - team (@user)" label doesn't fit the column.
    """
    if measure_text_with_emoji(text, font) <= max_width:
        return text
    suf_w = measure_text_with_emoji(suffix, font)
    out = ""
    cur_w = 0
    for piece, is_emoji in split_emoji_runs(text):
        piece_w = measure_text_with_emoji(piece, font)
        if cur_w + piece_w + suf_w <= max_width:
            out += piece
            cur_w += piece_w
            continue
        if is_emoji:
            break
        for ch in piece:
            ch_w = measure_text_with_emoji(ch, font)
            if cur_w + ch_w + suf_w > max_width:
                break
            out += ch
            cur_w += ch_w
        break
    return (out + suffix) if out != text else text


__all__ = [
    "EMOJI_FONT_PATH",
    "NATIVE_EMOJI_SIZE",
    "split_emoji_runs",
    "measure_text_with_emoji",
    "draw_text_with_emoji",
    "truncate_text_with_emoji",
]
