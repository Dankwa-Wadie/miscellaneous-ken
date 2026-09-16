#!/usr/bin/env python3
"""
scrape_reddit.py — Categorized Reddit research scraper.

Fetches posts from categorized subreddits using Reddit's public JSON endpoint,
handling rate-limiting headers (x-ratelimit-remaining, x-ratelimit-reset),
exponential backoff on HTTP 429, and exporting to CSV and JSON.

Usage:
    python3 scrape_reddit.py
    python3 scrape_reddit.py --category "AI & LLMs" --limit 5
    python3 scrape_reddit.py --sort top --limit 10
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("reddit_scraper")

ROOT = Path(__file__).resolve().parent

DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "Celebrity & Entertainment": [
        "movies", "popculturechat", "Fauxmoi", "BollyBlindsNGossip", "kpop"
    ],
    "Apple & PC Hardware": [
        "apple", "macbook", "macgaming", "pcgaming", "Windows11", "Alienware", "GamingLaptops"
    ],
    "Phones, Watches & Tech": [
        "Android", "iphone", "AppleWatch", "GalaxyWatch", "Watches", "gadgets", "technology"
    ],
    "AI & LLMs": [
        "ClaudeCode", "ClaudeAI", "ChatGPT", "artificial"
    ],
    "Comedy & Curiosities": [
        "funny", "dadjokes", "Jokes", "darkjokes", "todayilearned",
        "interestingasfuck", "interesting", "blackmagicfuckery", "nextfuckinglevel"
    ],
    "3D Modeling & Linux": [
        "3Dmodeling", "blender", "linux"
    ],
    "Wholesome & Memes": [
        "wholesome", "BeAmazed", "MadeMeSmile", "youseeingthisshit",
        "meme", "humor", "dashcams", "justGuysBeingDudes", "Unexpected"
    ]
}

DEFAULT_USER_AGENT = "MultiCategoryResearchScraper/1.0 (Educational Project; contact@example.com)"


def load_config_categories() -> dict[str, list[str]]:
    cfg_path = ROOT / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cats = cfg.get("discovery", {}).get("reddit_categories")
            if isinstance(cats, dict) and cats:
                return cats
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Could not read categories from config.json: %s", exc)
    return DEFAULT_CATEGORIES


def load_env_credentials() -> tuple[str, str]:
    """Read REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET from env or mken.env."""
    cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    env_file = ROOT / "mken.env"
    if (not cid or not secret) and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if k == "REDDIT_CLIENT_ID" and not cid:
                cid = v
            elif k == "REDDIT_CLIENT_SECRET" and not secret:
                secret = v
    return cid, secret


def get_oauth_token() -> str | None:
    cid, secret = load_env_credentials()
    if not cid or not secret or "REPLACE_ME" in (cid + secret):
        return None
    try:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(cid, secret),
            headers={"User-Agent": DEFAULT_USER_AGENT},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token")
        LOG.warning("Reddit OAuth failed (HTTP %d): %s", resp.status_code, resp.text[:120])
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Reddit OAuth error: %s", exc)
    return None


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def extract_reddit_media(post: dict[str, Any]) -> tuple[list[str], str, str, bool, bool]:
    """
    Extract direct media assets from a Reddit post object.

    Returns:
        (image_urls, video_url, primary_media_url, is_video, is_gallery)
    """
    image_urls: list[str] = []
    video_url: str = ""

    # A crosspost carries no media of its own — media, secure_media and
    # is_video are all empty and the url is a bare v.redd.it link. The
    # original post travels with it, so read the media off that instead.
    parents = post.get("crosspost_parent_list") or []
    if parents and not ((post.get("media") or {}).get("reddit_video")
                        or (post.get("secure_media") or {}).get("reddit_video")
                        or post.get("is_gallery")):
        parent = {k: v for k, v in parents[0].items() if k != "crosspost_parent_list"}
        return extract_reddit_media(parent)

    # 1. Video extraction
    for parent_key in ("media", "secure_media"):
        rv = (post.get(parent_key) or {}).get("reddit_video") or {}
        fb = rv.get("fallback_url") or rv.get("scrubber_media_url")
        if fb:
            video_url = fb.replace("&amp;", "&")
            break

    if not video_url:
        pv_vid = (post.get("preview") or {}).get("reddit_video_preview") or {}
        fb = pv_vid.get("fallback_url") or pv_vid.get("scrubber_media_url")
        if fb:
            video_url = fb.replace("&amp;", "&")

    dest = (post.get("url_overridden_by_dest") or post.get("url") or "").strip()
    if not video_url and dest:
        clean_dest = dest.split("?")[0].lower()
        if (clean_dest.endswith((".mp4", ".mov", ".webm"))
                or "packaged-media.redd.it" in dest or "v.redd.it" in dest):
            video_url = dest.replace("&amp;", "&")

    is_video = bool(video_url or post.get("is_video") or (post.get("media") or {}).get("reddit_video"))

    # 2. Image extraction (Galleries, Previews, Direct URLs)
    is_gallery = bool(post.get("is_gallery"))
    if is_gallery and isinstance(post.get("media_metadata"), dict):
        order = [i.get("media_id") for i in (post.get("gallery_data") or {}).get("items", [])]
        for mid in (order or list(post["media_metadata"].keys())):
            meta = post["media_metadata"].get(mid) or {}
            best = (meta.get("s") or {}).get("u") or (meta.get("s") or {}).get("gif")
            if best:
                clean_img = best.replace("&amp;", "&")
                if clean_img not in image_urls:
                    image_urls.append(clean_img)

    # Preview images
    previews = (post.get("preview") or {}).get("images") or []
    for pv in previews:
        src = (pv.get("source") or {}).get("url")
        if src:
            clean_pv = src.replace("&amp;", "&")
            if clean_pv not in image_urls:
                image_urls.append(clean_pv)

    # Direct image link
    if dest:
        clean_dest = dest.split("?")[0].lower()
        if clean_dest.endswith(IMAGE_EXTS) or "i.redd.it" in dest or "i.imgur.com" in dest:
            clean_direct = dest.replace("&amp;", "&")
            if clean_direct not in image_urls:
                image_urls.insert(0, clean_direct)

    # 3. Determine primary media URL (direct media asset, never subreddit link)
    if is_video and video_url:
        primary_media = video_url
    elif image_urls:
        primary_media = image_urls[0]
    elif dest and not any(p in dest for p in ("reddit.com/r/", "reddit.com/user/", "reddit.com/gallery/")):
        primary_media = dest
    else:
        primary_media = ""

    return image_urls, video_url, primary_media, is_video, is_gallery


def fetch_subreddit_posts(
    subreddit: str,
    category: str,
    limit: int = 5,
    sort: str = "hot",
    headers: dict[str, str] | None = None,
    token: str | None = None,
    retries: int = 3,
) -> list[dict[str, Any]]:
    """Fetch posts from a single subreddit with rate-limit pacing and retry logic."""
    if headers is None:
        headers = {"User-Agent": DEFAULT_USER_AGENT}
    if token:
        headers["Authorization"] = f"bearer {token}"

    sub_clean = subreddit.lstrip("/r/").strip("/")
    url = f"https://oauth.reddit.com/r/{sub_clean}/{sort}?limit={limit}" if token else f"https://www.reddit.com/r/{sub_clean}/{sort}.json?limit={limit}"

    for attempt in range(retries):
        try:
            response = requests.get(url, headers=headers, timeout=10)

            # Handle rate-limiting headers returned by Reddit
            remaining = float(response.headers.get("x-ratelimit-remaining", 10))
            reset_seconds = float(response.headers.get("x-ratelimit-reset", 1))

            if response.status_code == 200:
                payload = response.json()
                raw_posts = payload.get("data", {}).get("children", [])

                parsed_posts: list[dict[str, Any]] = []
                for p in raw_posts:
                    post = p.get("data", {})
                    if post.get("stickied") or post.get("over_18"):
                        continue

                    img_urls, vid_url, media_url, is_video, is_gallery = extract_reddit_media(post)

                    parsed_posts.append({
                        "category": category,
                        "subreddit": sub_clean,
                        "title": (post.get("title") or "").strip(),
                        "score": int(post.get("score", 0)),
                        "upvote_ratio": float(post.get("upvote_ratio", 0.0)),
                        "num_comments": int(post.get("num_comments", 0)),
                        "author": post.get("author", "[deleted]"),
                        "permalink": f"https://reddit.com{post.get('permalink', '')}",
                        "created_utc": int(post.get("created_utc", 0)),
                        "is_video": is_video,
                        "is_gallery": is_gallery,
                        "media_url": media_url,
                        "image_urls": img_urls,
                        "video_url": vid_url,
                        "selftext": (post.get("selftext") or "")[:500],
                    })

                # Pace requests if quota is getting low
                delay = (reset_seconds / remaining) if (remaining > 0 and remaining < 20) else 1.5
                time.sleep(delay)
                return parsed_posts

            elif response.status_code == 429:
                wait_time = max(reset_seconds, float(2 ** (attempt + 1)))
                LOG.warning("[429 Rate Limited] Sleeping for %.1fs before retrying r/%s...", wait_time, sub_clean)
                time.sleep(wait_time)

            elif response.status_code in (403, 404):
                LOG.info("[Skipped] r/%s is private, banned, or restricted (Status: %d)", sub_clean, response.status_code)
                return []

            else:
                LOG.warning("r/%s HTTP %d: %s", sub_clean, response.status_code, response.text[:120])
                time.sleep(1.5)

        except requests.RequestException as err:
            LOG.warning("[Connection Error] r/%s: %s (attempt %d/%d)", sub_clean, err, attempt + 1, retries)
            time.sleep(2)

    return []


def run_scraper(
    categories: dict[str, list[str]],
    limit: int = 5,
    sort: str = "hot",
    user_agent: str = DEFAULT_USER_AGENT,
    selected_category: str | None = None,
    csv_file: Path | None = None,
    json_file: Path | None = None,
) -> list[dict[str, Any]]:
    headers = {"User-Agent": user_agent}
    all_data: list[dict[str, Any]] = []

    target_cats = (
        {selected_category: categories[selected_category]}
        if selected_category and selected_category in categories
        else categories
    )

    token = get_oauth_token()
    if token:
        LOG.info("Using authenticated Reddit OAuth token")
    else:
        LOG.info("No Reddit credentials found — attempting unauthenticated public access")

    for category, subreddits in target_cats.items():
        LOG.info("--- Scraping Category: %s (%d subreddits) ---", category, len(subreddits))
        for sub in subreddits:
            LOG.info("  -> Fetching r/%s...", sub)
            posts = fetch_subreddit_posts(sub, category, limit=limit, sort=sort, headers=headers, token=token)
            all_data.extend(posts)

    # Export to CSV
    if csv_file and all_data:
        csv_file.parent.mkdir(parents=True, exist_ok=True)
        keys = list(all_data[0].keys())
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            dict_writer = csv.DictWriter(f, fieldnames=keys)
            dict_writer.writeheader()
            for row in all_data:
                csv_row = dict(row)
                if isinstance(csv_row.get("image_urls"), list):
                    csv_row["image_urls"] = ";".join(csv_row["image_urls"])
                dict_writer.writerow(csv_row)
        LOG.info("Saved %d posts to %s", len(all_data), csv_file)

    # Export to JSON
    if json_file:
        json_file.parent.mkdir(parents=True, exist_ok=True)
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(all_data, f, indent=2, ensure_ascii=False)
        LOG.info("Saved JSON payload to %s", json_file)

    return all_data


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-category Reddit Scraper")
    parser.add_argument("--category", help="Only scrape a specific category")
    parser.add_argument("--limit", type=int, default=5, help="Posts per subreddit (default: 5)")
    parser.add_argument("--sort", default="hot", choices=["hot", "new", "top", "rising"], help="Sort order (default: hot)")
    parser.add_argument("--csv", default="scraped_reddit_data.csv", help="CSV output filename")
    parser.add_argument("--json", default="scraped_reddit_data.json", help="JSON output filename")
    args = parser.parse_args()

    categories = load_config_categories()
    if args.category and args.category not in categories:
        LOG.error("Unknown category %r. Available categories: %s", args.category, list(categories.keys()))
        return 1

    run_scraper(
        categories=categories,
        limit=args.limit,
        sort=args.sort,
        selected_category=args.category,
        csv_file=ROOT / args.csv if args.csv else None,
        json_file=ROOT / args.json if args.json else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
