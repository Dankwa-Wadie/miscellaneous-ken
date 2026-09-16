#!/usr/bin/env python3
"""
render_story.py — the story-card format.

A different shape from render.py's video card:

    +------------------------------------------+
    | ~~~~~~ looping mascot video fills ~~~~~~~ |  1080x1920
    | ~~~~~~ the entire background ~~~~~~~~~~~~ |
    |  +------------------------------------+  |
    |  | (o) Miscellaneous Ken (v)          |  |  card floats on top
    |  |     @miscellaneousken              |  |
    |  |                                    |  |
    |  | Long story text, wrapped, in a     |  |  proportional face -
    |  | proportional face because it runs  |  |  monospace would wrap
    |  | to several lines.                  |  |  to ~20 chars a line
    |  |                                    |  |
    |  | +--------------+ +--------------+  |  |  one or two images,
    |  | |   image 1    | |   image 2    |  |  |  side by side
    |  | +--------------+ +--------------+  |  |
    |  |                                    |  |
    |  | | quoted commentary underneath    |  |  reply block, accent bar
    |  +------------------------------------+  |
    | ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ |
    +------------------------------------------+

The card is static, so it is baked into one PNG that is TRANSPARENT except
where the card sits. ffmpeg cover-crops the looping mascot to fill the whole
1080x1920 canvas and lays that PNG over it — one scale, one crop, one overlay.
A dimming scrim can be drawn behind the card so text stays readable over
whatever the video is doing.

Usage:
    python3 render_story.py --headline "..." --commentary "..." \\
        --images a.jpg b.jpg --mascot assets/mascot.mp4 --out out/story.mp4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

import render  # reuse fonts, avatar, badge, rounded masks, encoder choice

LOG = logging.getLogger("mken.render")

DEFAULT_STORY_LAYOUT: dict[str, Any] = {
    "canvas_width": 1080,
    "canvas_height": 1920,
    "background": "#000000",

    # -- card --------------------------------------------------------------
    "card_margin": 40,          # canvas edge -> card edge
    "card_padding": 44,         # card edge -> content
    "card_bg": "#16181C",
    "card_opacity": 1.0,        # <1 lets the video show faintly through the card
    "card_radius": 44,
    "card_top": 70,

    # -- header ------------------------------------------------------------
    "avatar_size": 84,
    "header_gap": 20,
    "name_size": 40,
    "handle_size": 32,
    "name_color": "#FFFFFF",
    "handle_color": "#8B98A5",
    "verified_color": "#1D9BF0",
    "verified_size": 36,
    "header_to_story": 34,

    # -- story text --------------------------------------------------------
    # Proportional, not monospace: this text runs to 250+ characters and a
    # monospace face would wrap it to a ragged column.
    "story_size": 42,
    "story_color": "#E7E9EA",
    "story_line_spacing": 12,
    "story_to_images": 34,

    # -- images ------------------------------------------------------------
    "image_gap": 16,
    "image_radius": 24,
    "image_max_height": 620,
    "images_to_commentary": 32,

    # -- commentary --------------------------------------------------------
    "commentary_size": 34,
    "commentary_color": "#C8CDD2",
    "commentary_line_spacing": 10,
    "commentary_bar_color": "#1D9BF0",
    "commentary_bar_width": 6,
    "commentary_bar_gap": 22,

    # -- background ---------------------------------------------------------
    # The mascot video fills the whole canvas. 0 = show it untouched;
    # higher values darken it so the card and text stay legible over busy or
    # bright footage. 0.45 is a reasonable starting point.
    "background_dim": 0.45,
    "vertical_bias": 0.42,   # 0.5 = card dead centre; lower lifts it

    # -- template ------------------------------------------------------------
    # "auto" picks per post (see resolve_story_template); "reaction_card" or
    # "classic" force one. Theme "auto" follows the template.
    "template": "auto",
    "theme": "auto",

    # -- caption card (full-bleed clip under a centred caption) ---------------
    # Wrap a phrase in *asterisks* to tint it with caption_accent.
    "caption_size": 52,
    "caption_line_spacing": 12,
    "caption_margin": 56,
    "caption_top": 96,
    "caption_gap": 28,
    "caption_bottom": 120,
    "caption_bg": "#000000",
    "caption_text": "#FFFFFF",
    "caption_handle": "#8B98A5",
    "caption_accent": "#F5B72C",
    "caption_avatar_size": 78,
    "caption_name_size": 38,
    "caption_handle_size": 30,
    "caption_verified_size": 32,
    "caption_header_gap": 18,

    # -- reaction card --------------------------------------------------------
    "reaction_headline_size": 56,
    "reaction_headline_spacing": 12,
    "reaction_gap": 30,
    # Off by default: a subscribe CTA suits a hand-made reaction post, not
    # every automated story the pipeline publishes overnight.
    "show_subscribe": False,
    "subscribe_color": "#FFD000",
    "subscribe_text_color": "#000000",
    "subscribe_text": "SUBSCRIBE!",
    "subscribe_size": 30,

    "mono_font": "",
    "ui_font": "",
}


@dataclass
class StoryResult:
    output: Path
    overlay: Path
    mascot_box: tuple[int, int, int, int]
    duration: float
    poster: Path | None = None
    template: str = "classic"


def wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """Greedy wrap on real glyph widths, preserving deliberate line breaks."""
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for w in words[1:]:
            trial = f"{cur} {w}"
            if draw.textbbox((0, 0), trial, font=font)[2] <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def fit_images(paths: list[str], max_w: int, max_h: int, gap: int) -> list[Image.Image]:
    """
    Load images and size them to a single row that fits max_w x max_h.

    Side-by-side comparison shots are the point of this format, so the row is
    sized to a common height and the whole row scaled down if it overflows —
    rather than cropping, which would defeat a before/after pair.
    """
    loaded: list[Image.Image] = []
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
            loaded.append(im)
        except (OSError, ValueError) as exc:
            LOG.warning("could not open image %s: %s", p, exc)
    if not loaded:
        return []

    target_h = min(max_h, max(im.height for im in loaded))
    scaled = [im.resize((max(1, int(im.width * target_h / im.height)), target_h),
                        Image.LANCZOS) for im in loaded]

    total_w = sum(im.width for im in scaled) + gap * (len(scaled) - 1)
    if total_w > max_w:
        factor = (max_w - gap * (len(scaled) - 1)) / sum(im.width for im in scaled)
        scaled = [im.resize((max(1, int(im.width * factor)),
                             max(1, int(im.height * factor))), Image.LANCZOS)
                  for im in scaled]
    return scaled


def paste_rounded(canvas: Image.Image, img: Image.Image, xy: tuple[int, int],
                  radius: int) -> None:
    mask = render.rounded_mask(img.width, img.height, radius)
    canvas.paste(img, xy, mask)


def size_images(image_paths: list[str], content_w: int, spare_h: int,
                layout: dict[str, Any]) -> list[Image.Image]:
    """
    Size a row of images to the space a card can spare.

    Both templates go through here. The readable text in a Reddit screenshot
    is the joke, so the fixed cap is only a floor — whichever is larger wins.
    """
    max_img_h = max(int(layout["image_max_height"]), spare_h)
    return fit_images(image_paths, content_w, max_img_h, int(layout["image_gap"]))


def resolve_story_template(layout: dict[str, Any], headline: str,
                           commentary: str, image_paths: list[str]) -> str:
    """
    Choose between the reaction card and the classic card.

    Derived only from the post's own content, so re-rendering a draft always
    lands on the same template — a counter or clock would silently restyle
    a video on edit.
    """
    choice = str(layout.get("template", "auto")).strip().lower()
    if choice in ("reaction_card", "classic", "caption_card"):
        return choice
    if len(image_paths) != 1:
        # Two images are a side-by-side comparison, and none leaves the
        # reaction card with no hero. Both are the classic card's job.
        return "classic"
    if commentary.strip():
        return "reaction_card"
    # Genuinely ambiguous: one image, no punchline text. Hash the headline so
    # the channel gets a mix while any single post stays where it landed.
    return ("reaction_card"
            if hashlib.sha1(headline.encode("utf-8")).digest()[0] % 2
            else "classic")


def resolve_theme(layout: dict[str, Any], template: str) -> str:
    """'auto' follows the template: the reaction card is a light tweet card."""
    theme = str(layout.get("theme", "auto")).strip().lower()
    if theme in ("light", "dark"):
        return theme
    return "light" if template == "reaction_card" else "dark"


REACTION_THEMES: dict[str, dict[str, str]] = {
    "light": {"card_bg": "#FFFFFF", "text": "#0F1419", "handle": "#536471",
              "commentary": "#0F1419", "rule": "#E3E6E8"},
    "dark": {"card_bg": "#16181C", "text": "#E7E9EA", "handle": "#8B98A5",
             "commentary": "#C8CDD2", "rule": "#2F3336"},
}


def build_reaction_overlay(
    headline: str,
    commentary: str,
    image_paths: list[str],
    account: render.Account,
    layout: dict[str, Any],
    out_path: Path,
    theme: str = "light",
) -> tuple[int, int, int, int]:
    """
    The reaction card: hook on top, hero image, identity bar, punchline.

    Same contract as build_story_overlay — a transparent PNG over the mascot,
    returning the video window — so composite() treats them identically.
    """
    palette = REACTION_THEMES.get(theme, REACTION_THEMES["light"])
    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    dim = float(layout.get("background_dim", 0.0))
    if dim > 0:
        draw.rectangle([0, 0, W, H], fill=(0, 0, 0, int(255 * min(dim, 1.0))))

    hook_font = render.load_font(render.UI_CANDIDATES,
                                 int(layout["reaction_headline_size"]),
                                 layout.get("ui_font", ""), role="reaction hook")
    name_font = render.load_font(render.UI_CANDIDATES, int(layout["name_size"]),
                                 layout.get("ui_font", ""), role="account name (bold)")
    handle_font = render.load_font(render.UI_REGULAR_CANDIDATES,
                                   int(layout["handle_size"]), role="@handle (regular)")
    comm_font = render.load_font(render.UI_REGULAR_CANDIDATES,
                                 int(layout["commentary_size"]), role="commentary")
    sub_font = render.load_font(render.UI_CANDIDATES, int(layout["subscribe_size"]),
                                layout.get("ui_font", ""), role="subscribe badge")

    m = int(layout["card_margin"])
    pad = int(layout["card_padding"])
    gap = int(layout["reaction_gap"])
    card_x, card_w = m, W - m * 2
    content_w = card_w - pad * 2
    card_top = int(layout["card_top"])
    card_max_bottom = H - card_top

    hook_lines = wrap(draw, headline, hook_font, content_w)
    hook_lh = int(layout["reaction_headline_size"]) + int(layout["reaction_headline_spacing"])
    hook_h = hook_lh * len(hook_lines)

    header_h = max(int(layout["avatar_size"]),
                   int(layout["name_size"]) + int(layout["handle_size"]) + 10)

    comm_lines = wrap(draw, commentary, comm_font, content_w) if commentary else []
    comm_lh = int(layout["commentary_size"]) + int(layout["commentary_line_spacing"])
    comm_h = comm_lh * len(comm_lines)

    def card_height(img_h: int) -> int:
        h = pad + hook_h
        if img_h:
            h += gap + img_h
        h += gap + header_h
        if comm_h:
            h += gap + comm_h
        return h + pad

    images = size_images(image_paths, content_w,
                         card_max_bottom - card_top - card_height(0) - gap, layout)
    img_h = max((im.height for im in images), default=0)

    guard = 0
    while images and card_top + card_height(img_h) > card_max_bottom and guard < 40:
        images = [im.resize((max(1, int(im.width * 0.94)),
                             max(1, int(im.height * 0.94))), Image.LANCZOS)
                  for im in images]
        img_h = max(im.height for im in images)
        guard += 1
    if guard:
        LOG.info("reaction image scaled down %d step(s) to fit the card", guard)

    card_h = card_height(img_h)
    bias = float(layout.get("vertical_bias", 0.42))
    card_top = max(20, int((H - card_h) * min(max(bias, 0.0), 1.0)))

    card_fill = palette["card_bg"]
    alpha = float(layout.get("card_opacity", 1.0))
    if alpha < 1.0:
        rgb = Image.new("RGB", (1, 1), card_fill).getpixel((0, 0))
        card_fill = rgb + (int(255 * max(0.0, min(alpha, 1.0))),)
    draw.rounded_rectangle([card_x, card_top, card_x + card_w, card_top + card_h],
                           radius=int(layout["card_radius"]), fill=card_fill)

    x = card_x + pad
    y = card_top + pad

    for line in hook_lines:
        draw.text((x, y), line, font=hook_font, fill=palette["text"])
        y += hook_lh

    if images:
        y += gap
        row_w = sum(im.width for im in images) + int(layout["image_gap"]) * (len(images) - 1)
        ix = x + (content_w - row_w) // 2
        for im in images:
            paste_rounded(canvas, im, (ix, y + (img_h - im.height) // 2),
                          int(layout["image_radius"]))
            ix += im.width + int(layout["image_gap"])
        y += img_h

    # Identity bar: avatar and name on the left, subscribe pill on the right.
    y += gap
    avatar_size = int(layout["avatar_size"])
    avatar = render.make_avatar(account.avatar, avatar_size, account.name,
                                trim=bool(layout.get("avatar_trim", True)),
                                zoom=float(layout.get("avatar_zoom", 1.0)))
    canvas.alpha_composite(avatar, (x, y + (header_h - avatar_size) // 2))

    tx = x + avatar_size + int(layout["header_gap"])
    ty = y + (header_h - (int(layout["name_size"]) + int(layout["handle_size"]) + 10)) // 2
    draw.text((tx, ty), account.name, font=name_font, fill=palette["text"])
    if account.verified:
        nw = draw.textbbox((0, 0), account.name, font=name_font)[2]
        badge = int(layout["verified_size"])
        render.draw_verified_badge(draw, cx=tx + nw + 12 + badge // 2,
                                   cy=ty + int(layout["name_size"]) * 0.55,
                                   size=badge, color=layout["verified_color"])
    draw.text((tx, ty + int(layout["name_size"]) + 10), account.handle,
              font=handle_font, fill=palette["handle"])

    if bool(layout.get("show_subscribe", False)):
        text = str(layout["subscribe_text"])
        tw = draw.textbbox((0, 0), text, font=sub_font)[2]
        bh = int(layout["subscribe_size"]) + 24
        bw = tw + 40
        bx = card_x + card_w - pad - bw
        by = y + (header_h - bh) // 2
        draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2,
                               fill=layout["subscribe_color"])
        draw.text((bx + 20, by + 12), text, font=sub_font,
                  fill=layout["subscribe_text_color"])
    y += header_h

    if comm_lines:
        y += gap
        for line in comm_lines:
            draw.text((x, y), line, font=comm_font, fill=palette["commentary"])
            y += comm_lh

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return (0, 0, W, H)


def build_story_overlay(
    headline: str,
    commentary: str,
    image_paths: list[str],
    account: render.Account,
    layout: dict[str, Any],
    out_path: Path,
) -> tuple[int, int, int, int]:
    """
    Draw the card as a transparent-background overlay.

    The mascot video fills the whole canvas underneath, so this PNG must be
    transparent everywhere the video should show through — the opposite of
    render.py's card, which is opaque with a hole punched in it.

    Returns the video window, which here is simply the whole canvas.
    """
    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # A scrim over the footage. Without it, white text and a dark card fight
    # whatever the video happens to be doing at that moment.
    dim = float(layout.get("background_dim", 0.0))
    if dim > 0:
        draw.rectangle([0, 0, W, H], fill=(0, 0, 0, int(255 * min(dim, 1.0))))

    name_font = render.load_font(render.UI_CANDIDATES, int(layout["name_size"]),
                                 layout.get("ui_font", ""), role="account name (bold)")
    handle_font = render.load_font(render.UI_REGULAR_CANDIDATES,
                                   int(layout["handle_size"]), role="@handle (regular)")
    story_font = render.load_font(render.UI_REGULAR_CANDIDATES,
                                  int(layout["story_size"]), role="story text")
    comm_font = render.load_font(render.UI_REGULAR_CANDIDATES,
                                 int(layout["commentary_size"]), role="commentary")

    m = int(layout["card_margin"])
    pad = int(layout["card_padding"])
    card_x, card_w = m, W - m * 2
    content_w = card_w - pad * 2

    # The card is the only thing on this layer, so it may use the full canvas
    # height minus a margin.
    card_top = int(layout["card_top"])
    card_max_bottom = H - card_top

    # ---- measure, shrinking the images if the card would overflow ---------
    header_h = max(int(layout["avatar_size"]),
                   int(layout["name_size"]) + int(layout["handle_size"]) + 10)
    story_lines = wrap(draw, headline, story_font, content_w) if headline.strip() else []
    story_lh = int(layout["story_size"]) + int(layout["story_line_spacing"])
    story_h = story_lh * len(story_lines)
    header_gap_h = int(layout["header_to_story"]) if story_lines else 0

    comm_lines = wrap(draw, commentary, comm_font,
                      content_w - int(layout["commentary_bar_width"])
                      - int(layout["commentary_bar_gap"])) if commentary else []
    comm_lh = int(layout["commentary_size"]) + int(layout["commentary_line_spacing"])
    comm_h = comm_lh * len(comm_lines)

    def card_height(img_h: int) -> int:
        h = pad + header_h + header_gap_h + story_h
        if img_h:
            h += int(layout["story_to_images"]) + img_h
        if comm_h:
            h += int(layout["images_to_commentary"]) + comm_h
        return h + pad

    # Screenshots usually *are* the joke — the readable text lives inside them
    # — so give them every pixel the card can spare rather than a fixed cap.
    # Width and the shrink loop below still bound the result, so a wide image
    # is unaffected and a pair simply splits the width as it always did.
    images = size_images(image_paths, content_w,
                         card_max_bottom - card_top - card_height(0)
                         - int(layout["story_to_images"]), layout)
    img_h = max((im.height for im in images), default=0)

    # Images are the only elastic element — text must stay legible.
    guard = 0
    while images and card_top + card_height(img_h) > card_max_bottom and guard < 40:
        img_h = int(img_h * 0.94)
        images = [im.resize((max(1, int(im.width * 0.94)),
                             max(1, int(im.height * 0.94))), Image.LANCZOS)
                  for im in images]
        img_h = max(im.height for im in images)
        guard += 1
    if guard:
        LOG.info("images scaled down %d step(s) to fit the card", guard)

    card_h = card_height(img_h)

    # Position the card over the video. Slightly above centre keeps it clear
    # of the UI chrome Shorts overlays along the bottom of the screen.
    bias = float(layout.get("vertical_bias", 0.42))
    card_top = max(20, int((H - card_h) * min(max(bias, 0.0), 1.0)))

    # ---- draw -------------------------------------------------------------
    card_fill = layout["card_bg"]
    alpha = float(layout.get("card_opacity", 1.0))
    if alpha < 1.0:
        rgb = Image.new("RGB", (1, 1), card_fill).getpixel((0, 0))
        card_fill = rgb + (int(255 * max(0.0, min(alpha, 1.0))),)
    draw.rounded_rectangle([card_x, card_top, card_x + card_w, card_top + card_h],
                           radius=int(layout["card_radius"]), fill=card_fill)

    x = card_x + pad
    y = card_top + pad

    avatar_size = int(layout["avatar_size"])
    avatar = render.make_avatar(account.avatar, avatar_size, account.name,
                                trim=bool(layout.get("avatar_trim", True)),
                                zoom=float(layout.get("avatar_zoom", 1.0)))
    canvas.alpha_composite(avatar, (x, y + (header_h - avatar_size) // 2))

    tx = x + avatar_size + int(layout["header_gap"])
    ty = y + (header_h - (int(layout["name_size"]) + int(layout["handle_size"]) + 10)) // 2
    draw.text((tx, ty), account.name, font=name_font, fill=layout["name_color"])
    if account.verified:
        nw = draw.textbbox((0, 0), account.name, font=name_font)[2]
        badge = int(layout["verified_size"])
        render.draw_verified_badge(draw, cx=tx + nw + 12 + badge // 2,
                                   cy=ty + int(layout["name_size"]) * 0.55,
                                   size=badge, color=layout["verified_color"])
    draw.text((tx, ty + int(layout["name_size"]) + 10), account.handle,
              font=handle_font, fill=layout["handle_color"])

    y += header_h + header_gap_h
    for line in story_lines:
        draw.text((x, y), line, font=story_font, fill=layout["story_color"])
        y += story_lh

    if images:
        y += int(layout["story_to_images"])
        row_w = sum(im.width for im in images) + int(layout["image_gap"]) * (len(images) - 1)
        ix = x + (content_w - row_w) // 2
        for im in images:
            paste_rounded(canvas, im, (ix, y + (img_h - im.height) // 2),
                          int(layout["image_radius"]))
            ix += im.width + int(layout["image_gap"])
        y += img_h

    if comm_lines:
        y += int(layout["images_to_commentary"])
        bar_w = int(layout["commentary_bar_width"])
        draw.rounded_rectangle([x, y, x + bar_w, y + comm_h - int(layout["commentary_line_spacing"])],
                               radius=bar_w // 2, fill=layout["commentary_bar_color"])
        cx = x + bar_w + int(layout["commentary_bar_gap"])
        cy = y
        for line in comm_lines:
            draw.text((cx, cy), line, font=comm_font, fill=layout["commentary_color"])
            cy += comm_lh

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    # The video window is the entire canvas: the mascot is the background.
    return (0, 0, W, H)


def render_story(
    headline: str,
    out: str | Path,
    commentary: str = "",
    images: list[str] | None = None,
    mascot: str | Path | None = None,
    config: dict[str, Any] | None = None,
    duration: float = 12.0,
    music: str | Path | None = None,
    music_start: float = 0.0,
    poster: str | Path | None = None,
    workdir: str | Path | None = None,
) -> StoryResult:
    config = config or {}
    layout = {**DEFAULT_STORY_LAYOUT, **(config.get("story_layout") or {})}
    acct = config.get("account") or {}

    avatar = acct.get("avatar", "")
    if avatar and not os.path.isabs(avatar) and not os.path.exists(avatar):
        cand = Path(__file__).resolve().parent / avatar
        avatar = str(cand) if cand.exists() else avatar
    account = render.Account(
        name=acct.get("name", render.Account.name),
        handle=acct.get("handle", render.Account.handle),
        avatar=avatar,
        verified=bool(acct.get("verified", True)),
    )

    out = Path(out)
    workdir = Path(workdir or out.parent / "work")
    workdir.mkdir(parents=True, exist_ok=True)
    overlay = workdir / f"{out.stem}_story_overlay.png"

    template = resolve_story_template(layout, headline, commentary, images or [])
    theme = resolve_theme(layout, template)
    LOG.info("Story template: %s (%s theme)", template, theme)
    if template == "reaction_card":
        box = build_reaction_overlay(headline, commentary, images or [], account,
                                     layout, overlay, theme)
    else:
        box = build_story_overlay(headline, commentary, images or [], account,
                                  layout, overlay)

    if not mascot:
        raise RuntimeError(
            "no mascot loop configured — set story.mascot in config.json to a "
            "short looping video (the cat)."
        )
    mascot = Path(mascot)
    if not mascot.exists():
        raise RuntimeError(f"mascot loop not found: {mascot}")

    dur = render.composite(
        video=mascot,
        overlay=overlay,
        box=box,
        layout={**render.DEFAULT_LAYOUT, **layout},
        out=out,
        max_seconds=duration,
        poster=Path(poster) if poster else None,
        config=config,
        music=Path(music) if music else None,
        music_start=music_start,
        loop_video=True,    # the mascot is a short loop, not the content
        source_audio=False,  # …and its silent track must not displace the music
    )
    return StoryResult(output=out, overlay=overlay, mascot_box=box,
                       duration=dur, poster=Path(poster) if poster else None,
                       template=template)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a Miscellaneous Ken story card.")
    ap.add_argument("--headline", required=True)
    ap.add_argument("--commentary", default="")
    ap.add_argument("--images", nargs="*", default=[])
    ap.add_argument("--mascot", required=True, help="looping video (the cat)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--duration", type=float, default=12.0)
    ap.add_argument("--music", default=None)
    ap.add_argument("--poster", default=None)
    ap.add_argument("--template", choices=["auto", "reaction_card", "classic"],
                    default=None, help="override story_layout.template")
    ap.add_argument("--theme", choices=["auto", "light", "dark"], default=None,
                    help="override story_layout.theme")
    ap.add_argument("--subscribe", action="store_true",
                    help="show the subscribe badge on a reaction card")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg: dict[str, Any] = {}
    if Path(args.config).exists():
        cfg = json.loads(Path(args.config).read_text())

    overrides = {k: v for k, v in (("template", args.template),
                                   ("theme", args.theme)) if v}
    if args.subscribe:
        overrides["show_subscribe"] = True
    if overrides:
        cfg["story_layout"] = {**(cfg.get("story_layout") or {}), **overrides}

    r = render_story(headline=args.headline, out=args.out,
                     commentary=args.commentary, images=args.images,
                     mascot=args.mascot, config=cfg, duration=args.duration,
                     music=args.music, poster=args.poster)
    print(f"rendered {r.output} ({r.duration:.1f}s) mascot box={r.mascot_box}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
