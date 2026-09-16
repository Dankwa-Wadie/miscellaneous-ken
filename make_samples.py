#!/usr/bin/env python3
"""
make_samples.py — render (and optionally upload) one sample of each format.

Exists because the normal pipeline picks its own stories via Claude, which
needs API credits. This drives the same rendering code directly so you can
see both formats on the channel before the editorial layer is paid for.

    # story card, subject chosen by a Wikipedia search
    python3 make_samples.py story --query "practical effects filmmaking"

    # video card, from a clip you have chosen yourself
    python3 make_samples.py video --url "https://www.youtube.com/watch?v=..."

Add --upload to send the result to YouTube (privacy comes from config.json,
currently "private").
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import agent
import render
import render_story

LOG = logging.getLogger("mken")
ROOT = Path(__file__).resolve().parent


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text())


def pick_story(query: str, cfg: dict) -> agent.Candidate | None:
    """First searchable article that has a FREE image and enough text."""
    free_only = bool(cfg.get("discovery", {}).get("free_images_only", True))
    cands = agent.discover_wikipedia_search([query], per_query=10, free_only=free_only)
    if not cands:
        LOG.error("nothing found for %r — try a different query", query)
        return None
    # Prefer a longer extract: it makes a better card than a one-line stub.
    cands.sort(key=lambda c: len(c.summary), reverse=True)
    return cands[0]


def do_story(args, cfg) -> int:
    c = pick_story(args.query, cfg)
    if not c:
        return 1

    print(f"\n  subject : {c.url}")
    print(f"  text    : {c.title[:150]}")
    print(f"  image   : {c.images[0][:90]}")
    print(f"  licence : {c.attribution}\n")

    imgs = agent.download_images(c.images[:2], ROOT / "work", "sample_story")
    if not imgs:
        LOG.error("image download failed")
        return 1

    story_cfg = cfg.get("story", {})
    out = ROOT / "out" / "sample_story.mp4"
    result = render_story.render_story(
        headline=args.headline or c.title,
        commentary=args.commentary or "",
        images=[str(p) for p in imgs],
        mascot=story_cfg.get("mascot", "assets/mascot.mp4"),
        out=out,
        config=cfg,
        duration=float(story_cfg.get("duration_seconds", 12)),
        music=agent.pick_music(args.mood, cfg),
        poster=ROOT / "out" / "sample_story.png",
        workdir=ROOT / "work",
    )
    print(f"rendered {result.output}  ({result.duration:.1f}s)")
    print(f"poster   {ROOT / 'out' / 'sample_story.png'}  <- look at this first\n")

    if args.upload:
        desc = (f"{args.commentary or ''}\n\nSource: {c.url}\n"
                f"Image: {c.attribution}\n\n#Shorts").strip()
        return _upload(result.output, (args.headline or c.title)[:90], desc, cfg, args)
    return 0


def do_video(args, cfg) -> int:
    posting = cfg.get("posting", {})
    max_seconds = int(posting.get("max_clip_seconds", 30))

    clip, err = agent.download_clip(args.url, ROOT / "work", max_seconds,
                                    cookies_file=posting.get("cookies_file", ""))
    if not clip:
        LOG.error("download failed: %s", err)
        return 1

    out = ROOT / "out" / "sample_video.mp4"
    result = render.render_card(
        video=clip,
        headline=args.headline,
        out=out,
        config=cfg,
        max_seconds=max_seconds,
        poster=ROOT / "out" / "sample_video.png",
        workdir=ROOT / "work",
        music=agent.pick_music(args.mood, cfg) if args.music else None,
    )
    print(f"rendered {result.output}  ({result.duration:.1f}s)")
    print(f"poster   {ROOT / 'out' / 'sample_video.png'}  <- look at this first\n")

    if args.upload:
        desc = f"Source: {args.url}\n\n#Shorts"
        return _upload(result.output, args.headline[:90], desc, cfg, args)
    return 0


def _upload(path: Path, title: str, description: str, cfg: dict, args) -> int:
    privacy = cfg.get("posting", {}).get("privacy", "private")
    print(f"uploading as {privacy!r}…")
    vid = agent.upload_youtube(path, f"{title} #Shorts", description, cfg,
                               ROOT / args.token)
    if not vid:
        return 1
    url = f"https://youtube.com/watch?v={vid}"
    print(f"\n  {url}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--headline", default="")
    common.add_argument("--mood", default="neutral")
    common.add_argument("--upload", action="store_true")
    common.add_argument("--token", default="token.json")
    common.add_argument("-v", "--verbose", action="store_true")

    s = sub.add_parser("story", parents=[common], help="image/story card")
    s.add_argument("--query", required=True, help="Wikipedia search for the subject")
    s.add_argument("--commentary", default="", help="the quoted line under the images")

    v = sub.add_parser("video", parents=[common], help="clip card")
    v.add_argument("--url", required=True, help="source clip URL for yt-dlp")
    v.add_argument("--music", action="store_true",
                   help="mix a music bed under the clip's own audio")

    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("mken").setLevel(logging.DEBUG if args.verbose else logging.INFO)

    cfg = load_config()
    if args.mode == "story":
        return do_story(args, cfg)
    return do_video(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
