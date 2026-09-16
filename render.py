#!/usr/bin/env python3
"""
render.py — Miscellaneous Ken card renderer.

Takes a raw video clip + a headline and composites it into the channel's
visual format:

    +------------------------------------------+
    |                                          |   black canvas, 1080x1920
    |   (o)  Miscellaneous Ken  (v)            |   header: avatar, name,
    |        @miscellaneousken                 |   verified tick, @handle
    |                                          |
    |   BOLD MONOSPACE ALL-CAPS                |   headline, wrapped
    |   HEADLINE GOES HERE                     |
    |                                          |
    |   +----------------------------------+   |
    |   |                                  |   |   source clip in a rounded
    |   |        source video clip         |   |   rect with a blue border
    |   |                                  |   |
    |   +----------------------------------+   |
    |                                          |
    +------------------------------------------+

The whole content block (header + headline + video) is measured first, then
vertically centred on the canvas.

Strategy: Pillow draws a single full-canvas RGBA overlay PNG containing
everything except the video pixels (the video window is left transparent).
ffmpeg then scales/crops the clip into the window, rounds its corners with a
generated alpha mask, and overlays the PNG on top.

Usage:
    python3 render.py --video clip.mp4 --headline "SOMETHING HAPPENED" \
        --out out/post.mp4 [--config config.json] [--max-seconds 30] \
        [--poster poster.png]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required: pip install Pillow")

LOG = logging.getLogger("mken.render")

# YouTube Shorts accept up to three minutes since October 2024.
SHORTS_MAX_SECONDS = 180.0


# --------------------------------------------------------------------------
# Layout defaults. Every value here can be overridden by config.json's
# "layout" object.
# --------------------------------------------------------------------------

DEFAULT_LAYOUT: dict[str, Any] = {
    "canvas_width": 1080,
    "canvas_height": 1920,
    "background": "#000000",
    "side_margin": 72,          # left/right padding for header + headline
    "video_margin": None,       # left/right padding for the clip (None = side_margin)
    # 0.5 centres the block; lower values raise it, pushing the dead space to
    # the bottom of the canvas instead of splitting it evenly above and below.
    "vertical_bias": 0.5,
    # -- header ------------------------------------------------------------
    "avatar_size": 96,
    "header_gap": 22,           # avatar -> text gap
    "name_size": 44,            # px font size for the display name
    "handle_size": 36,
    "name_color": "#FFFFFF",
    "handle_color": "#8B98A5",
    "verified_color": "#1D9BF0",
    "verified_size": 40,
    "header_to_headline": 48,   # vertical gap header -> headline
    # -- headline ----------------------------------------------------------
    "headline_size": 76,
    "headline_color": "#FFFFFF",
    "headline_line_spacing": 14,
    "headline_wrap_chars": 0,   # 0 = auto-fit to width
    "headline_uppercase": True,
    "headline_to_video": 52,    # vertical gap headline -> video frame
    # -- video frame -------------------------------------------------------
    "background_zoom": 1.0,     # >1 zooms in; use with the anchors to crop a
    "background_anchor_x": 0.5, # watermark out of frame (0 = left, 1 = right)
    "background_anchor_y": 0.5, # (0 = top, 1 = bottom)
    "video_aspect": "16:9",     # aspect of the video window (or "1:1", "4:5", "9:16", "auto")
    "video_fit": "cover",       # "cover" (crop), "contain" (letterbox/pad without cutoff), or "auto"
    "corner_radius": 40,
    "border_width": 6,
    "border_color": "#1D9BF0",
    # -- fonts -------------------------------------------------------------
    "mono_font": "",            # explicit path wins; else auto-detected
    "ui_font": "",
}

# Font candidates as (path, face_index). The index matters: macOS ships fonts
# as .ttc collections, and index 0 is the REGULAR weight. Asking Pillow for
# Menlo.ttc without an index silently gives you Menlo Regular — which looks
# fine until you compare it to the bold the design calls for. Standalone .ttf
# files are always index 0.
MONO_CANDIDATES: list[tuple[str, int]] = [
    ("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", 0),
    ("/Library/Fonts/Courier New Bold.ttf", 0),
    ("/System/Library/Fonts/Menlo.ttc", 1),          # 1 = Menlo Bold
    ("/System/Library/Fonts/SFNSMono.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0),
    ("/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf", 0),
    ("/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf", 0),
]

# Bold UI face for the account name.
UI_CANDIDATES: list[tuple[str, int]] = [
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
    ("/Library/Fonts/Arial Bold.ttf", 0),
    ("/System/Library/Fonts/HelveticaNeue.ttc", 1),  # 1 = Helvetica Neue Bold
    ("/System/Library/Fonts/SFNS.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 0),
]

# Regular UI face for the @handle.
UI_REGULAR_CANDIDATES: list[tuple[str, int]] = [
    ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
    ("/System/Library/Fonts/HelveticaNeue.ttc", 0),
    ("/System/Library/Fonts/SFNS.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 0),
]

# Records what each role actually resolved to, for --list-fonts.
_RESOLVED: dict[str, str] = {}


def load_font(
    candidates: list[tuple[str, int]],
    size: int,
    explicit: str = "",
    role: str = "",
) -> ImageFont.FreeTypeFont:
    """
    Load the first available face, honouring the collection index.

    An explicit path from config.json wins; a '#N' suffix on it selects a face
    within a .ttc (e.g. "/System/Library/Fonts/Menlo.ttc#1").
    """
    attempts: list[tuple[str, int]] = []
    if explicit:
        path, _, idx = explicit.partition("#")
        attempts.append((path, int(idx) if idx.isdigit() else 0))
    attempts += candidates

    for path, index in attempts:
        if not os.path.exists(path):
            continue
        try:
            font = ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
        if role:
            name = " ".join(str(n) for n in font.getname() if n)
            _RESOLVED[role] = f"{name}  ←  {path}#{index}"
        return font

    if role:
        _RESOLVED[role] = "PILLOW DEFAULT BITMAP FONT — no candidate found!"
    # Last resort: bitmap default (ignores size, but never crashes a render).
    return ImageFont.load_default()


def parse_aspect(spec: str) -> float:
    """'16:9' -> 1.777…; also accepts a bare float string."""
    spec = str(spec).strip()
    if ":" in spec:
        w, h = spec.split(":", 1)
        return float(w) / float(h)
    return float(spec)


@dataclass
class Account:
    name: str = "Miscellaneous Ken"
    handle: str = "@miscellaneousken"
    avatar: str = ""
    verified: bool = True


@dataclass
class RenderResult:
    output: Path
    overlay: Path
    video_box: tuple[int, int, int, int]  # x, y, w, h
    duration: float
    poster: Path | None = None
    mood: str = "neutral"
    music: Path | None = None


# --------------------------------------------------------------------------
# Overlay drawing
# --------------------------------------------------------------------------

def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def wrap_headline(
    text: str,
    font,
    max_width: int,
    draw: ImageDraw.ImageDraw,
    forced_chars: int = 0,
) -> list[str]:
    """Greedy word wrap that measures real glyph widths."""
    if forced_chars:
        return textwrap.wrap(text, width=forced_chars) or [""]

    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if _text_size(draw, trial, font)[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def draw_verified_badge(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    """A scalloped blue disc with a white check — the familiar verified mark."""
    r = size / 2
    # Scalloped outline: 8 lobes around the circle.
    lobes = 8
    lobe_r = r * 0.30
    for i in range(lobes):
        angle = (2 * math.pi / lobes) * i
        lx = cx + math.cos(angle) * (r - lobe_r * 0.55)
        ly = cy + math.sin(angle) * (r - lobe_r * 0.55)
        draw.ellipse([lx - lobe_r, ly - lobe_r, lx + lobe_r, ly + lobe_r], fill=color)
    draw.ellipse([cx - r * 0.82, cy - r * 0.82, cx + r * 0.82, cy + r * 0.82], fill=color)

    # Check mark.
    w = max(2, int(size * 0.10))
    draw.line(
        [
            (cx - r * 0.34, cy + r * 0.03),
            (cx - r * 0.08, cy + r * 0.30),
            (cx + r * 0.38, cy - r * 0.28),
        ],
        fill="#FFFFFF",
        width=w,
        joint="curve",
    )


def trim_uniform_border(img: Image.Image, tolerance: int = 12) -> Image.Image:
    """
    Crop a uniform border (usually white) so the subject fills the circle.

    Profile pictures are often exported with generous padding, which a circular
    crop then shrinks further — the subject ends up a small mark in a large
    disc. Only trims when all four corners agree on a background colour, so an
    image with real content at its edges is left alone.
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    corners = [rgb.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    first = corners[0]
    if any(max(abs(a - b) for a, b in zip(first, c)) > tolerance for c in corners):
        return img  # edges disagree — probably a real photo, leave it

    bg = Image.new("RGB", rgb.size, first)
    from PIL import ImageChops
    diff = ImageChops.difference(rgb, bg).convert("L").point(lambda p: 255 if p > tolerance else 0)
    box = diff.getbbox()
    if not box:
        return img

    # Square up around the subject, with a little breathing room.
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = max(x1 - x0, y1 - y0) / 2 * 1.12
    half = max(half, 8)
    left, top = int(max(0, cx - half)), int(max(0, cy - half))
    right, bottom = int(min(w, cx + half)), int(min(h, cy + half))
    return img.crop((left, top, right, bottom))


def make_avatar(path: str, size: int, fallback_letter: str, trim: bool = True,
                zoom: float = 1.0) -> Image.Image:
    """Circular avatar from a file, or a lettered placeholder disc."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)

    if path and os.path.exists(path):
        try:
            src = Image.open(path).convert("RGBA")
            if trim:
                src = trim_uniform_border(src)
            # Cover-crop to a square, then resize.
            sw, sh = src.size
            side = min(sw, sh)
            # zoom > 1 crops in before resizing, which is what a wide subject
            # needs: a circular mask clips the corners of a square crop, so a
            # figure with broad shoulders reads as a small head unless you
            # push in past them.
            side = max(8, int(side / max(zoom, 0.01)))
            src = src.crop((
                (sw - side) // 2,
                (sh - side) // 2,
                (sw - side) // 2 + side,
                (sh - side) // 2 + side,
            )).resize((size, size), Image.LANCZOS)
            img.paste(src, (0, 0))
            img.putalpha(mask)
            return img
        except OSError:
            pass

    # Placeholder: dark grey disc with the first letter of the account name.
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size - 1, size - 1], fill="#2F3336")
    font = load_font(UI_CANDIDATES, int(size * 0.5))
    letter = (fallback_letter or "K")[0].upper()
    box = draw.textbbox((0, 0), letter, font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
        letter,
        font=font,
        fill="#E7E9EA",
    )
    return img


def rounded_mask(w: int, h: int, radius: int, supersample: int = 4) -> Image.Image:
    """Anti-aliased rounded-rectangle alpha mask."""
    big = Image.new("L", (w * supersample, h * supersample), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, w * supersample - 1, h * supersample - 1],
        radius=radius * supersample,
        fill=255,
    )
    return big.resize((w, h), Image.LANCZOS)


# The reaction card paints the whole canvas as the card, so it carries its own
# colours rather than the channel's dark layout defaults.
REACTION_THEMES: dict[str, dict[str, str]] = {
    "light": {"bg": "#FFFFFF", "text": "#0F1419", "handle": "#536471",
              "commentary": "#0F1419"},
    "dark": {"bg": "#0B0B0D", "text": "#FFFFFF", "handle": "#8B98A5",
             "commentary": "#C8CDD2"},
}


def split_accents(text: str) -> list[tuple[str, bool]]:
    """
    Split "compare *Tony Stark* to" into runs, flagging the accented ones.

    Asterisks are the markup because they survive a plain text field and read
    as emphasis even to someone who has never seen the convention.
    """
    runs: list[tuple[str, bool]] = []
    for index, chunk in enumerate(text.split('*')):
        if chunk:
            runs.append((chunk, index % 2 == 1))
    return runs


def tokenize_runs(runs):
    """
    Group runs into words, where a word may span an accent boundary.

    "*the GOAT*." has to stay one word, or wrapping inserts a space and the
    full stop drifts away from the phrase it belongs to.
    """
    tokens: list[list[tuple[str, bool]]] = []
    current: list[tuple[str, bool]] = []
    for chunk, accent in runs:
        for part in re.split(r'(\s+)', chunk):
            if not part:
                continue
            if part.isspace():
                if current:
                    tokens.append(current)
                    current = []
            else:
                current.append((part, accent))
    if current:
        tokens.append(current)
    return tokens


def token_width(draw, token, font):
    return sum(_text_size(draw, text, font)[0] for text, _ in token)


def wrap_runs(draw, runs, font, max_w):
    """Word-wrap styled runs, keeping each word's segments together."""
    space = _text_size(draw, ' ', font)[0]
    lines: list[list[list[tuple[str, bool]]]] = []
    current: list[list[tuple[str, bool]]] = []
    width = 0.0
    for token in tokenize_runs(runs):
        tw = token_width(draw, token, font)
        extra = tw if not current else space + tw
        if current and width + extra > max_w:
            lines.append(current)
            current, width = [token], tw
        else:
            current.append(token)
            width += extra
    if current:
        lines.append(current)
    return lines


def build_caption_overlay(
    headline: str,
    account: Account,
    layout: dict[str, Any],
    out_path: Path,
    video_aspect: str | float | None = None,
) -> tuple[int, int, int, int]:
    """
    Caption card: compact header, centred caption, full-bleed clip.

    Unlike the other two the clip runs edge to edge with no border or rounded
    corners, so the card reads as a post rather than a framed video.
    """
    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    margin = int(layout.get("caption_margin", 56))
    content_w = W - margin * 2

    canvas = Image.new("RGBA", (W, H), layout.get("caption_bg", "#000000"))
    draw = ImageDraw.Draw(canvas)

    cap_size = int(layout.get("caption_size", 52))
    cap_font = load_font(UI_CANDIDATES, cap_size, layout.get("ui_font", ""),
                         role="caption card text")
    name_font = load_font(UI_CANDIDATES, int(layout.get("caption_name_size", 38)),
                          layout.get("ui_font", ""), role="account name (bold)")
    handle_font = load_font(UI_REGULAR_CANDIDATES,
                            int(layout.get("caption_handle_size", 30)),
                            role="@handle (regular)")

    avatar_size = int(layout.get("caption_avatar_size", 78))
    header_h = max(avatar_size,
                   int(layout.get("caption_name_size", 38))
                   + int(layout.get("caption_handle_size", 30)) + 8)

    lines = wrap_runs(draw, split_accents(headline), cap_font, content_w) \
        if headline.strip() else []
    line_h = cap_size + int(layout.get("caption_line_spacing", 12))
    caption_h = line_h * len(lines)

    top = int(layout.get("caption_top", 96))
    gap = int(layout.get("caption_gap", 28))
    y = top

    avatar = make_avatar(account.avatar, avatar_size, account.name,
                         trim=bool(layout.get("avatar_trim", True)),
                         zoom=float(layout.get("avatar_zoom", 1.0)))
    canvas.alpha_composite(avatar, (margin, y + (header_h - avatar_size) // 2))
    tx = margin + avatar_size + int(layout.get("caption_header_gap", 18))
    ty = y + (header_h - (int(layout.get("caption_name_size", 38))
                          + int(layout.get("caption_handle_size", 30)) + 8)) // 2
    draw.text((tx, ty), account.name, font=name_font,
              fill=layout.get("caption_text", "#FFFFFF"))
    if account.verified:
        nw = _text_size(draw, account.name, name_font)[0]
        badge = int(layout.get("caption_verified_size", 32))
        draw_verified_badge(draw, cx=tx + nw + 12 + badge // 2,
                            cy=ty + int(layout.get("caption_name_size", 38)) * 0.55,
                            size=badge, color=layout["verified_color"])
    draw.text((tx, ty + int(layout.get("caption_name_size", 38)) + 8),
              account.handle, font=handle_font,
              fill=layout.get("caption_handle", "#8B98A5"))
    y += header_h + (gap if lines else gap)

    accent = layout.get("caption_accent", "#F5B72C")
    body = layout.get("caption_text", "#FFFFFF")
    space_w = _text_size(draw, ' ', cap_font)[0]
    for line in lines:
        width = sum(token_width(draw, t, cap_font) for t in line) \
            + space_w * (len(line) - 1)
        x = (W - width) // 2
        for index, token in enumerate(line):
            if index:
                x += space_w
            for text, is_accent in token:
                draw.text((x, y), text, font=cap_font,
                          fill=accent if is_accent else body)
                x += _text_size(draw, text, cap_font)[0]
        y += line_h
    if lines:
        y += gap

    raw_aspect = video_aspect or layout.get("video_aspect", "16:9")
    aspect = 1.7777777777777777 if str(raw_aspect).strip().lower() == "auto" \
        else parse_aspect(str(raw_aspect))
    # Full bleed where it fits: the clip takes the canvas width, and if that
    # would run past the bottom it shrinks on BOTH axes. Clamping height alone
    # changes the window's shape, which makes composite letterbox the clip.
    available = H - y - int(layout.get("caption_bottom", 120))
    frame_w = W
    frame_h = int(round(frame_w / aspect))
    if frame_h > available:
        frame_h = max(200, available)
        frame_w = min(W, max(200, int(round(frame_h * aspect))))
    frame_x = (W - frame_w) // 2
    frame_y = y
    canvas.paste((0, 0, 0, 0), (frame_x, frame_y),
                 Image.new("L", (frame_w, frame_h), 255))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return frame_x, frame_y, frame_w, frame_h


def build_reaction_overlay(
    headline: str,
    commentary: str,
    account: Account,
    layout: dict[str, Any],
    out_path: Path,
    video_aspect: str | float | None = None,
    theme: str = "light",
) -> tuple[int, int, int, int]:
    """
    Reaction-card variant of build_overlay: hook, clip, identity bar, punchline.

    Same contract as build_overlay — an opaque canvas with a rounded hole for
    the clip, returning that window — so composite() is unchanged.
    """
    palette = REACTION_THEMES.get(theme, REACTION_THEMES["light"])
    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    margin = int(layout["side_margin"])
    content_w = W - margin * 2

    canvas = Image.new("RGBA", (W, H), palette["bg"])
    draw = ImageDraw.Draw(canvas)

    hook_size = int(layout.get("reaction_headline_size", 60))
    hook_font = load_font(UI_CANDIDATES, hook_size, layout.get("ui_font", ""),
                          role="reaction hook")
    name_font = load_font(UI_CANDIDATES, int(layout["name_size"]),
                          layout.get("ui_font", ""), role="account name (bold)")
    handle_font = load_font(UI_REGULAR_CANDIDATES, int(layout["handle_size"]),
                            role="@handle (regular)")
    comm_font = load_font(UI_REGULAR_CANDIDATES,
                          int(layout.get("reaction_commentary_size", 38)),
                          role="commentary")
    sub_font = load_font(UI_CANDIDATES, int(layout.get("subscribe_size", 30)),
                         layout.get("ui_font", ""), role="subscribe badge")

    def wrap_text(text: str, font) -> list[str]:
        out: list[str] = []
        for para in text.split("\n"):
            words = para.split()
            if not words:
                continue
            cur = words[0]
            for word in words[1:]:
                trial = f"{cur} {word}"
                if _text_size(draw, trial, font)[0] <= content_w:
                    cur = trial
                else:
                    out.append(cur)
                    cur = word
            out.append(cur)
        return out

    gap = int(layout.get("reaction_gap", 34))
    hook_lines = wrap_text(headline, hook_font) if headline.strip() else []
    hook_lh = hook_size + int(layout.get("reaction_headline_spacing", 12))
    hook_h = hook_lh * len(hook_lines)
    hook_gap = gap if hook_lines else 0

    header_h = max(int(layout["avatar_size"]),
                   int(layout["name_size"]) + int(layout["handle_size"]) + 12)

    comm_lines = wrap_text(commentary, comm_font) if commentary else []
    comm_lh = int(layout.get("reaction_commentary_size", 38)) + 10
    comm_h = comm_lh * len(comm_lines)

    raw_aspect = video_aspect or layout.get("video_aspect", "16:9")
    aspect = 1.7777777777777777 if str(raw_aspect).strip().lower() == "auto" \
        else parse_aspect(str(raw_aspect))
    vid_margin = layout.get("video_margin")
    vid_margin = margin if vid_margin is None else int(vid_margin)
    frame_w = W - vid_margin * 2
    frame_h = int(round(frame_w / aspect))

    fixed = hook_h + hook_gap + gap + header_h + (gap + comm_h if comm_h else 0)
    max_frame_h = H - fixed - 80
    if frame_h > max_frame_h:
        frame_h = max(100, max_frame_h)
        frame_w = max(100, int(round(frame_h * aspect)))
        if frame_w > W - vid_margin * 2:
            frame_w = W - vid_margin * 2
            frame_h = int(round(frame_w / aspect))
    frame_x = (W - frame_w) // 2

    block_h = fixed + frame_h
    bias = float(layout.get("vertical_bias", 0.5))
    top = max(0, int((H - block_h) * min(max(bias, 0.0), 1.0)))

    y = top
    for line in hook_lines:
        draw.text((margin, y), line, font=hook_font, fill=palette["text"])
        y += hook_lh

    y += hook_gap
    frame_y = y
    radius = int(layout["corner_radius"])
    hole = rounded_mask(frame_w, frame_h, radius)
    canvas.paste((0, 0, 0, 0), (frame_x, frame_y), hole)
    draw.rounded_rectangle(
        [frame_x, frame_y, frame_x + frame_w - 1, frame_y + frame_h - 1],
        radius=radius, outline=layout["border_color"],
        width=int(layout["border_width"]))
    y += frame_h + gap

    avatar_size = int(layout["avatar_size"])
    avatar = make_avatar(account.avatar, avatar_size, account.name,
                         trim=bool(layout.get("avatar_trim", True)),
                         zoom=float(layout.get("avatar_zoom", 1.0)))
    canvas.alpha_composite(avatar, (margin, y + (header_h - avatar_size) // 2))
    text_x = margin + avatar_size + int(layout["header_gap"])
    name_y = y + (header_h - (int(layout["name_size"]) + int(layout["handle_size"]) + 12)) // 2
    draw.text((text_x, name_y), account.name, font=name_font, fill=palette["text"])
    if account.verified:
        name_w = _text_size(draw, account.name, name_font)[0]
        badge = int(layout["verified_size"])
        draw_verified_badge(draw, cx=text_x + name_w + 14 + badge // 2,
                            cy=name_y + int(layout["name_size"]) * 0.55,
                            size=badge, color=layout["verified_color"])
    draw.text((text_x, name_y + int(layout["name_size"]) + 12), account.handle,
              font=handle_font, fill=palette["handle"])

    if bool(layout.get("show_subscribe", False)):
        label = str(layout.get("subscribe_text", "SUBSCRIBE!"))
        tw = _text_size(draw, label, sub_font)[0]
        bh = int(layout.get("subscribe_size", 30)) + 24
        bw = tw + 40
        bx = W - margin - bw
        by = y + (header_h - bh) // 2
        draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2,
                               fill=layout.get("subscribe_color", "#FFD000"))
        draw.text((bx + 20, by + 12), label, font=sub_font,
                  fill=layout.get("subscribe_text_color", "#000000"))
    y += header_h

    if comm_lines:
        y += gap
        for line in comm_lines:
            draw.text((margin, y), line, font=comm_font, fill=palette["commentary"])
            y += comm_lh

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return frame_x, frame_y, frame_w, frame_h


def build_overlay(
    headline: str,
    account: Account,
    layout: dict[str, Any],
    out_path: Path,
    video_aspect: str | float | None = None,
) -> tuple[int, int, int, int]:
    """
    Draw the full-canvas overlay PNG. Returns the video window box
    (x, y, w, h) in canvas pixels — the region left transparent for ffmpeg
    to fill with the clip.
    """
    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    margin = int(layout["side_margin"])
    content_w = W - margin * 2

    # Opaque background, not transparent. The overlay then IS the whole card:
    # ffmpeg only has to pad the clip onto a canvas and lay this on top, and
    # the rounded corners come free from the hole punched below.
    canvas = Image.new("RGBA", (W, H), layout["background"])
    draw = ImageDraw.Draw(canvas)

    mono = load_font(MONO_CANDIDATES, int(layout["headline_size"]),
                     layout.get("mono_font", ""), role="headline (bold mono)")
    name_font = load_font(UI_CANDIDATES, int(layout["name_size"]),
                          layout.get("ui_font", ""), role="account name (bold)")
    handle_font = load_font(UI_REGULAR_CANDIDATES, int(layout["handle_size"]),
                            role="@handle (regular)")

    # ---- measure -------------------------------------------------------
    avatar_size = int(layout["avatar_size"])
    header_h = max(
        avatar_size,
        int(layout["name_size"]) + int(layout["handle_size"]) + 12,
    )

    text = headline.upper() if layout.get("headline_uppercase", True) else headline
    # An empty headline is deliberate on posts whose text is already burned
    # into the media; give its space to the clip instead of leaving a gap.
    lines = wrap_headline(
        text, mono, content_w, draw, int(layout.get("headline_wrap_chars") or 0)
    ) if text.strip() else []
    line_h = int(layout["headline_size"]) + int(layout["headline_line_spacing"])
    headline_h = line_h * len(lines)
    header_gap_h = int(layout["header_to_headline"]) if lines else 0

    border = int(layout["border_width"])
    raw_aspect = video_aspect or layout.get("video_aspect", "16:9")
    if str(raw_aspect).lower() == "auto":
        aspect = 1.7777777777777777
    else:
        aspect = parse_aspect(str(raw_aspect))

    vid_margin = layout.get("video_margin")
    vid_margin = margin if vid_margin is None else int(vid_margin)
    frame_w = W - vid_margin * 2
    frame_h = int(round(frame_w / aspect))

    # Guard against frame_h overflowing the canvas
    max_frame_h = H - (
        header_h
        + header_gap_h
        + headline_h
        + int(layout["headline_to_video"])
        + 80
    )
    if frame_h > max_frame_h:
        frame_h = max_frame_h
        frame_w = max(100, int(round(frame_h * aspect)))

    frame_x = (W - frame_w) // 2

    block_h = (
        header_h
        + header_gap_h
        + headline_h
        + int(layout["headline_to_video"])
        + frame_h
    )
    bias = float(layout.get("vertical_bias", 0.5))
    top = max(0, int((H - block_h) * min(max(bias, 0.0), 1.0)))

    # ---- header --------------------------------------------------------
    y = top
    avatar = make_avatar(account.avatar, avatar_size, account.name,
                         trim=bool(layout.get("avatar_trim", True)),
                         zoom=float(layout.get("avatar_zoom", 1.0)))
    canvas.alpha_composite(avatar, (margin, y + (header_h - avatar_size) // 2))

    text_x = margin + avatar_size + int(layout["header_gap"])
    name_y = y + (header_h - (int(layout["name_size"]) + int(layout["handle_size"]) + 12)) // 2
    draw.text((text_x, name_y), account.name, font=name_font, fill=layout["name_color"])

    if account.verified:
        name_w = _text_size(draw, account.name, name_font)[0]
        badge = int(layout["verified_size"])
        draw_verified_badge(
            draw,
            cx=text_x + name_w + 14 + badge // 2,
            cy=name_y + int(layout["name_size"]) * 0.55,
            size=badge,
            color=layout["verified_color"],
        )

    draw.text(
        (text_x, name_y + int(layout["name_size"]) + 12),
        account.handle,
        font=handle_font,
        fill=layout["handle_color"],
    )

    # ---- headline ------------------------------------------------------
    y = top + header_h + header_gap_h
    for line in lines:
        draw.text((margin, y), line, font=mono, fill=layout["headline_color"])
        y += line_h

    # ---- video frame ---------------------------------------------------
    frame_y = top + header_h + header_gap_h + headline_h + int(
        layout["headline_to_video"]
    )
    radius = int(layout["corner_radius"])

    # Punch a rounded transparent hole where the video goes, then stroke the
    # border on top of the hole's edge.
    hole = rounded_mask(frame_w, frame_h, radius)
    canvas.paste((0, 0, 0, 0), (frame_x, frame_y), hole)
    draw.rounded_rectangle(
        [frame_x, frame_y, frame_x + frame_w - 1, frame_y + frame_h - 1],
        radius=radius,
        outline=layout["border_color"],
        width=border,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return frame_x, frame_y, frame_w, frame_h


# --------------------------------------------------------------------------
# ffmpeg compositing
# --------------------------------------------------------------------------

_ENCODERS_CACHE: str | None = None


def has_encoder(name: str) -> bool:
    global _ENCODERS_CACHE
    if _ENCODERS_CACHE is None:
        try:
            _ENCODERS_CACHE = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, check=False,
            ).stdout
        except FileNotFoundError:
            _ENCODERS_CACHE = ""
    return name in (_ENCODERS_CACHE or "")


def video_encoder_args(config: dict[str, Any]) -> list[str]:
    """
    Pick an encoder. Defaults to hardware on macOS, which is the whole ball game:
    software x264 at preset 'medium' took ~5.5 minutes for a 30-second
    1080x1920 render; VideoToolbox does the same work in a fraction of that
    because it runs on the Apple silicon media engine rather than the CPU.
    """
    posting = config.get("posting", {})
    choice = posting.get("encoder", "auto")
    bitrate = str(posting.get("video_bitrate", "6M"))
    preset = posting.get("x264_preset", "veryfast")
    crf = str(posting.get("crf", 20))

    if choice == "auto":
        choice = "h264_videotoolbox" if has_encoder("h264_videotoolbox") else "libx264"

    if choice == "h264_videotoolbox" and has_encoder("h264_videotoolbox"):
        # VideoToolbox has no CRF; it is bitrate-targeted.
        return ["-c:v", "h264_videotoolbox", "-b:v", bitrate, "-profile:v", "high"]

    return ["-c:v", "libx264", "-preset", preset, "-crf", crf]


def has_audio_stream(video: Path) -> bool:
    """Whether the source has an audio track that should be preserved."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return bool(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def probe_duration(video: Path) -> float:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(video),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return 0.0


def probe_video_dimensions(video: Path) -> tuple[int, int]:
    """Probe video width and height in pixels."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(video),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if "x" in out:
            parts = out.split("x")
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 0, 0


def composite(
    video: Path,
    overlay: Path,
    box: tuple[int, int, int, int],
    layout: dict[str, Any],
    out: Path,
    max_seconds: float | None = None,
    start_seconds: float = 0.0,
    poster: Path | None = None,
    config: dict[str, Any] | None = None,
    music: Path | None = None,
    music_start: float = 0.0,
    loop_video: bool = False,
    source_audio: bool = True,
) -> float:
    """Scale/crop the clip into the window, round its corners, overlay the card."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH — install it (brew install ffmpeg)")

    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    x, y, w, h = box

    fit_mode = str(layout.get("video_fit", "cover")).lower()
    if fit_mode == "contain":
        fchain = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={layout['background']},"
            f"pad={W}:{H}:{x}:{y}:color={layout['background']},"
            f"setsar=1[stage];"
            f"[stage][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
        )
    else:
        # Zoom + anchored crop. A watermark burned into a corner of the source can
        # be pushed outside the frame by zooming in and biasing the crop away from
        # it — anchor 0.5/0.5 is centred, lower values move the visible window
        # toward the top-left and so discard the bottom-right.
        zoom = max(1.0, float(layout.get("background_zoom", 1.0)))
        ax = min(max(float(layout.get("background_anchor_x", 0.5)), 0.0), 1.0)
        ay = min(max(float(layout.get("background_anchor_y", 0.5)), 0.0), 1.0)
        sw, sh = int(w * zoom), int(h * zoom)

        fchain = (
            f"[0:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}:(in_w-out_w)*{ax}:(in_h-out_h)*{ay},"
            f"pad={W}:{H}:{x}:{y}:color={layout['background']},"
            f"setsar=1[stage];"
            f"[stage][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
        )

    audio_cfg = (config or {}).get("audio", {})
    music_vol = float(audio_cfg.get("music_volume", 0.35))
    fade = float(audio_cfg.get("fade_out_seconds", 1.5))
    # Preserve existing sound, including music. Apply this at render time so
    # re-rendering older drafts cannot layer their saved music over the source.
    # A decorative background (the story mascot) is not content: its track is
    # silent filler, and deferring to it would mute the post entirely.
    src_has_audio = has_audio_stream(video) and source_audio
    if src_has_audio:
        music = None

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start_seconds:
        cmd += ["-ss", str(start_seconds)]
    # A short mascot loop must repeat to fill the target duration. Without
    # this, -shortest truncates the whole render to the mascot's length — a
    # 5-second clip silently caps every post at 5 seconds.
    if loop_video:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(video), "-loop", "1", "-i", str(overlay)]

    # Music is input 2 when present. -stream_loop -1 repeats it indefinitely so
    # a 20-second track still covers a 30-second clip; -shortest then trims it.
    if music:
        cmd += ["-stream_loop", "-1"]
        # Seeking before -i skips an intro; looping then repeats from that
        # point, so a track cued past its build-up never falls back to the top.
        if music_start > 0:
            cmd += ["-ss", str(music_start)]
        cmd += ["-i", str(music)]

    if max_seconds:
        cmd += ["-t", str(max_seconds)]

    amap: list[str] = []
    if music:
        dur = max_seconds or probe_duration(video) or 30.0
        fade_start = max(0.0, float(dur) - fade)
        fchain += (
            f";[2:a]volume={music_vol},afade=t=out:st={fade_start:.2f}:d={fade},"
            f"aresample=48000[a]"
        )
        amap = ["-map", "[a]"]
    elif src_has_audio:
        amap = ["-map", "0:a"]

    cmd += [
        "-filter_complex", fchain,
        "-map", "[v]",
    ] + amap
    cmd += video_encoder_args(config or {})
    cmd += [
        # fps is a direct multiplier on filter+encode work: 24 is 20% less
        # work than 30 and is perfectly normal for short-form video.
        "-pix_fmt", "yuv420p",
        "-r", str((config or {}).get("posting", {}).get("fps", 30)),
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-movflags", "+faststart",
        "-shortest",
        str(out),
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr.strip()}")
    LOG.info("encoded in %.1fs using %s",
             time.time() - started,
             " ".join(video_encoder_args(config or {})[:2]))

    if poster:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(out), "-vf", "select=eq(n\\,15)", "-vframes", "1", str(poster)],
            capture_output=True, text=True,
        )

    return probe_duration(out)


# --------------------------------------------------------------------------
# Public entry point (also used by agent.py)
# --------------------------------------------------------------------------

def render_card(
    video: str | Path,
    headline: str,
    out: str | Path,
    config: dict[str, Any] | None = None,
    max_seconds: float | None = None,
    start_seconds: float = 0.0,
    poster: str | Path | None = None,
    workdir: str | Path | None = None,
    music: str | Path | None = None,
    music_start: float = 0.0,
    mood: str | None = None,
    commentary: str = "",
) -> RenderResult:
    config = config or {}
    layout = {**DEFAULT_LAYOUT, **(config.get("layout") or {})}
    acct_cfg = config.get("account") or {}

    # Resolve a relative avatar path against the project directory, not the
    # current working directory. Otherwise running the agent from anywhere
    # else silently falls back to the lettered placeholder — a failure that
    # looks like a design choice rather than a missing file.
    avatar = acct_cfg.get("avatar", "")
    if avatar and not os.path.isabs(avatar) and not os.path.exists(avatar):
        candidate = Path(__file__).resolve().parent / avatar
        if candidate.exists():
            avatar = str(candidate)
        else:
            LOG.warning("avatar %r not found (tried %s) — using placeholder",
                        acct_cfg.get("avatar"), candidate)

    account = Account(
        name=acct_cfg.get("name", Account.name),
        handle=acct_cfg.get("handle", Account.handle),
        avatar=avatar,
        verified=bool(acct_cfg.get("verified", True)),
    )

    video = Path(video)
    out = Path(out)
    workdir = Path(workdir or out.parent / "work")
    workdir.mkdir(parents=True, exist_ok=True)

    vw, vh = probe_video_dimensions(video)
    aspect_spec = str(layout.get("video_aspect", "16:9")).lower()
    fit_spec = str(layout.get("video_fit", "cover")).lower()
    seen_mood = ""

    if aspect_spec == "auto" or fit_spec in ("auto", "smart"):
        try:
            import agent
            crop_info = agent.analyze_visual_crop(video, config=config, workdir=workdir)
            seen_mood = crop_info.get("mood", "")
            if fit_spec in ("auto", "smart"):
                layout["video_fit"] = crop_info.get("fit_mode", "contain")
            if aspect_spec == "auto":
                layout["video_aspect"] = crop_info.get("aspect_ratio", "1:1")
            if "anchor_y" in crop_info and "background_anchor_y" not in (config.get("layout") or {}):
                layout["background_anchor_y"] = crop_info["anchor_y"]
            if "anchor_x" in crop_info and "background_anchor_x" not in (config.get("layout") or {}):
                layout["background_anchor_x"] = crop_info["anchor_x"]
            LOG.info("AI crop analysis: fit=%s, aspect=%s, anchor=(%.2f, %.2f), mood=%s — %s",
                     layout.get("video_fit"), layout.get("video_aspect"),
                     layout.get("background_anchor_x", 0.5), layout.get("background_anchor_y", 0.5),
                     seen_mood or "-", crop_info.get("reasoning", ""))
        except Exception as exc:
            LOG.warning("Could not perform visual crop analysis: %s — falling back to geometry", exc)
            if vh > 0 and (vw / vh) < 0.85:
                if fit_spec in ("auto", "smart"):
                    layout["video_fit"] = "contain"
                if aspect_spec == "auto":
                    layout["video_aspect"] = "1:1"
            elif aspect_spec == "auto":
                layout["video_aspect"] = "16:9"

    # 180s is the Shorts ceiling (YouTube raised it from 60s in Oct 2024);
    # a caller's cap may only tighten it.
    max_seconds = min(float(max_seconds or SHORTS_MAX_SECONDS), SHORTS_MAX_SECONDS)
    available = probe_duration(video) - start_seconds
    if has_audio_stream(video):
        import agent
        from clip_ending import resolve_ending
        LOG.info('Checking for a complete audio ending before rendering')
        chosen, heard_mood = resolve_ending(
            video, start_seconds, available, max_seconds, config,
            agent.available_moods(config))
        # Finding a sentence boundary is for clips that must be cut short. A
        # clip that already fits the cap has a real ending of its own, and
        # trimming it back only lops off the last beat.
        if available > 0 and available <= max_seconds:
            if chosen < available - 0.05:
                LOG.info('Clip fits the %.1fs cap — keeping all %.2fs rather '
                         'than ending at %.2fs', max_seconds, available, chosen)
            max_seconds = available
        else:
            max_seconds = chosen
        LOG.info('Render ending: %s seconds (source end if unspecified)', max_seconds)
        if heard_mood:
            LOG.info('Audio mood: %s', heard_mood)
        # Sound reads mood better than one still frame, so it wins — but
        # 'neutral' is the no-signal answer, and a confident read from either
        # source beats it.
        specific = [m for m in (heard_mood, seen_mood) if m and m != "neutral"]
        seen_mood = specific[0] if specific else (heard_mood or seen_mood)
    elif available > 0:
        # The overlay is an endless -loop 1 image, so -t would hold the last
        # video frame to fill the cap. A silent clip has no ending check to
        # shorten it, so stop at the source instead of freezing.
        max_seconds = min(max_seconds, available)

    # A caller that names a mood is asking us to choose; one that passes
    # neither mood nor track wants silence, as before.
    if music is None and mood is not None:
        import agent
        mood = seen_mood or mood
        music = agent.pick_music(mood, config)
        LOG.info('Music mood: %s (%s)', mood, 'from media' if seen_mood else 'from editorial')

    overlay = workdir / f"{out.stem}_overlay.png"

    # The story templates live in render_story; here "reaction_card" restyles
    # the video card itself. Only an explicit choice switches it — "auto" keeps
    # the channel's established look rather than restyling every clip.
    story_cfg = (config.get("story_layout") or {})
    template = str(story_cfg.get("template", "auto")).strip().lower()
    if template == "caption_card":
        merged = {**layout, **{k: v for k, v in story_cfg.items()
                               if k.startswith("caption_")}}
        # Full bleed means the clip keeps its own shape; a forced window
        # aspect would crop it against the edges of the canvas.
        hero = (vw / vh) if vw > 0 and vh > 0 else None
        LOG.info("Video template: caption_card")
        box = build_caption_overlay(headline, account, merged, overlay,
                                    video_aspect=hero)
    elif template == "reaction_card":
        theme = str(story_cfg.get("theme", "auto")).strip().lower()
        if theme not in ("light", "dark"):
            theme = "light"
        merged = {**layout, **{k: v for k, v in story_cfg.items()
                               if k in ("reaction_headline_size", "reaction_gap",
                                        "reaction_commentary_size", "show_subscribe",
                                        "subscribe_color", "subscribe_text_color",
                                        "subscribe_text", "subscribe_size")}}
        # In the reaction card the clip is the whole post, so the hero window
        # takes the source's own aspect: nothing is cropped away and there are
        # no letterbox bars, because the window and the clip are the same shape.
        hero_aspect = (vw / vh) if vw > 0 and vh > 0 else None
        LOG.info("Video template: reaction_card (%s theme, aspect %s)", theme,
                 f"{hero_aspect:.3f} from source" if hero_aspect
                 else layout.get("video_aspect"))
        box = build_reaction_overlay(headline, commentary, account, merged,
                                     overlay, video_aspect=hero_aspect, theme=theme)
    else:
        box = build_overlay(headline, account, layout, overlay)
    duration = composite(
        video, overlay, box, layout, out,
        max_seconds=max_seconds,
        start_seconds=start_seconds,
        poster=Path(poster) if poster else None,
        config=config,
        music=Path(music) if music else None,
        music_start=music_start,
    )
    return RenderResult(
        output=out,
        overlay=overlay,
        video_box=box,
        duration=duration,
        poster=Path(poster) if poster else None,
        mood=seen_mood or mood or "neutral",
        music=Path(music) if music else None,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a Miscellaneous Ken card video.")
    ap.add_argument("--video", help="source clip")
    ap.add_argument("--headline", help="headline text")
    ap.add_argument("--out", help="output mp4 path")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--list-fonts", action="store_true",
                    help="show which font files this machine resolves, then exit")
    ap.add_argument("--max-seconds", type=float, default=None)
    ap.add_argument("--start-seconds", type=float, default=0.0)
    ap.add_argument("--poster", default=None, help="also write a still frame here")
    args = ap.parse_args()

    config: dict[str, Any] = {}
    cfg_path = Path(args.config)
    if cfg_path.exists():
        config = json.loads(cfg_path.read_text())

    if args.list_fonts:
        layout = {**DEFAULT_LAYOUT, **(config.get("layout") or {})}
        load_font(MONO_CANDIDATES, 76, layout.get("mono_font", ""),
                  role="headline (bold mono)")
        load_font(UI_CANDIDATES, 44, layout.get("ui_font", ""),
                  role="account name (bold)")
        load_font(UI_REGULAR_CANDIDATES, 36, role="@handle (regular)")
        print("Fonts resolved on this machine:\n")
        for role, what in _RESOLVED.items():
            print(f"  {role:24}  {what}")
        print("\nIf a face says Regular where you expect Bold, set "
              "layout.mono_font / layout.ui_font in config.json\n"
              "to an explicit path (append '#1' etc. to pick a face inside a .ttc).")
        return 0

    missing = [f for f in ("video", "headline", "out") if not getattr(args, f)]
    if missing:
        ap.error("missing required argument(s): " + ", ".join("--" + m for m in missing))

    result = render_card(
        video=args.video,
        headline=args.headline,
        out=args.out,
        config=config,
        max_seconds=args.max_seconds,
        start_seconds=args.start_seconds,
        poster=args.poster,
    )
    print(f"rendered {result.output}  ({result.duration:.2f}s)  video box={result.video_box}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
