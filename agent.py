#!/usr/bin/env python3
"""
agent.py — Miscellaneous Ken posting pipeline.

    discover  ->  pick (Claude)  ->  download (yt-dlp)  ->  render  ->  upload

Discovery and story-picking both live here on purpose: n8n's job is only to
run this script on a schedule (Execute Command / cron), so the whole editorial
loop stays in one testable, version-controlled place.

    python3 agent.py --dry-run        # everything except the upload
    python3 agent.py                  # full run, posts to YouTube Shorts
    python3 agent.py --limit 1 -v     # one clip, verbose

State lives in state.json: every URL ever seen or posted, so a story is never
picked twice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
import render
import studio_store as studio

LOG = logging.getLogger("mken")

# How many dropped connections to ride out before giving up on one upload.
MAX_UPLOAD_DROPS = 6

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_STATE = ROOT / "state.json"
WORK_DIR = ROOT / "work"
OUT_DIR = ROOT / "out"

# Wikimedia (and Reddit) require a descriptive agent identifying the operator;
# generic agents get rate-limited or blocked.
USER_AGENT = os.environ.get(
    "MKEN_USER_AGENT",
    "MiscellaneousKen/1.0 (YouTube Shorts bot; contact via youtube.com/@Miscellaneous_ken)",
)


def _ssl_context() -> "ssl.SSLContext":
    """
    A TLS context that actually has root certificates.

    Python installed from python.org on macOS does NOT use the system keychain
    and ships with no CA bundle, so every https request dies with
    CERTIFICATE_VERIFY_FAILED until you run "Install Certificates.command".
    Using certifi's bundle when it's available makes this work regardless of
    how Python was installed — including under launchd, where nobody has run
    that installer.
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = None


# ==========================================================================
# Data model
# ==========================================================================

@dataclass
class Candidate:
    """One discovered story, before Claude has judged it."""
    title: str
    url: str
    source: str                  # "rss:BBC", "reddit:r/interestingasfuck", …
    video_url: str = ""          # the thing yt-dlp should download
    summary: str = ""
    score: int = 0               # upvotes / engagement where available
    published: str = ""          # ISO8601 if known
    # "video" -> render.py's clip card; "story" -> render_story.py's image card.
    kind: str = "video"
    licence: str = ""            # "public-domain"/"cc" if the SOURCE was declared
                                 # trusted in config; "" means it must be verified
    images: list[str] = field(default_factory=list)   # story format only
    commentary: str = ""                              # the quoted reply
    attribution: str = ""        # credit line — required for CC-BY material

    @property
    def key(self) -> str:
        """Stable dedupe key — the canonical URL, hashed."""
        u = (self.video_url or self.url).strip().lower().rstrip("/")
        return hashlib.sha1(u.encode()).hexdigest()[:16]


@dataclass
class Pick:
    """A candidate Claude chose, with the copy it wrote."""
    candidate: Candidate
    headline: str
    caption: str
    reason: str = ""
    mood: str = "neutral"        # drives the background music choice


# ==========================================================================
# State
# ==========================================================================

class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"seen": {}, "posted": {}, "runs": []}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                LOG.warning("state.json unreadable — starting fresh")
        self.data.setdefault("seen", {})
        self.data.setdefault("posted", {})
        self.data.setdefault("runs", [])
        self.data.setdefault("failed", {})
        self.data.setdefault("reddit_cursor", {})

    def get_reddit_cursor(self, category: str) -> int:
        return int(self.data.setdefault("reddit_cursor", {}).get(category, 0))

    def set_reddit_cursor(self, category: str, cursor: int) -> None:
        self.data.setdefault("reddit_cursor", {})[category] = cursor

    def is_seen(self, c: Candidate, max_attempts: int = 3) -> bool:
        if c.key in self.data["seen"]:
            return True
        # A candidate that failed to download is NOT permanently burned — an
        # upcoming livestream becomes a real video once it airs, and a
        # transient network error deserves another go. Only give up after
        # max_attempts, so a genuinely broken URL can't be retried forever.
        fail = self.data["failed"].get(c.key)
        return bool(fail and fail.get("attempts", 0) >= max_attempts)

    def mark_failed(self, c: Candidate, reason: str) -> None:
        rec = self.data["failed"].setdefault(c.key, {"attempts": 0})
        rec["attempts"] = rec.get("attempts", 0) + 1
        rec["url"] = c.video_url or c.url
        rec["title"] = c.title
        rec["reason"] = reason[:300]
        rec["at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    def mark_seen(self, c: Candidate) -> None:
        self.data["seen"][c.key] = {
            "url": c.video_url or c.url,
            "title": c.title,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def mark_posted(self, pick: Pick, video_id: str, dry_run: bool) -> None:
        self.data["posted"][pick.candidate.key] = {
            "url": pick.candidate.video_url or pick.candidate.url,
            "headline": pick.headline,
            "youtube_id": video_id,
            "dry_run": dry_run,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def log_run(self, summary: dict[str, Any]) -> None:
        self.data["runs"].append(summary)
        self.data["runs"] = self.data["runs"][-100:]

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


# ==========================================================================
# Discovery
# ==========================================================================

def _http_get(url: str, timeout: int = 20, retries: int = 2) -> str:
    """
    GET a URL as text, retrying transient rate limits.

    Wikimedia's feed API rate-limits an unauthenticated caller readily, and a
    single 429 there silently costs the whole story pipeline — the run keeps
    going and just has nothing to post a story card from. Retrying on 429/503
    (honouring Retry-After when the server sends one) turns a lost run into a
    two-second pause. Anything else fails immediately: retrying a 404 is just
    a slower 404.
    """
    import urllib.error
    import urllib.request

    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _ssl_context()

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == retries:
                raise
            wait = 2.0 * (attempt + 1)
            try:
                wait = max(wait, min(float(exc.headers.get("Retry-After", 0)), 30.0))
            except (TypeError, ValueError):
                pass
            LOG.debug("%s from %s — retrying in %.0fs (%d/%d)",
                      exc.code, url.split("/")[2], wait, attempt + 1, retries)
            time.sleep(wait)
        except urllib.error.URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise RuntimeError(
                    "TLS certificate verification failed — this Python has no CA bundle. "
                    "Fix it with:  pip install certifi   (or run "
                    "'/Applications/Python 3.x/Install Certificates.command' on macOS)"
                ) from exc
            raise
    raise RuntimeError(f"unreachable: retries exhausted for {url}")


def discover_rss(feeds: list[str], per_feed: int = 10) -> list[Candidate]:
    """Parse RSS/Atom feeds. Uses feedparser when installed, else a regex fallback."""
    out: list[Candidate] = []
    try:
        import feedparser  # type: ignore
    except ImportError:
        feedparser = None

    for feed in feeds:
        label = urlparse(feed).netloc or feed
        before = len(out)
        try:
            # Always fetch ourselves: urllib honours proxy env vars and lets us
            # set a User-Agent, both of which feedparser's own fetcher skips.
            xml = _http_get(feed)
            if feedparser:
                parsed = feedparser.parse(xml)
                entries = parsed.entries[:per_feed]
                for e in entries:
                    out.append(Candidate(
                        title=getattr(e, "title", "").strip(),
                        url=getattr(e, "link", "").strip(),
                        source=f"rss:{label}",
                        summary=re.sub(r"<[^>]+>", "", getattr(e, "summary", ""))[:400],
                        published=getattr(e, "published", ""),
                    ))
            else:
                items = re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", xml, re.S)[:per_feed]
                for item in items:
                    t = re.search(r"<title[^>]*>(.*?)</title>", item, re.S)
                    l = re.search(r"<link[^>]*?href=\"(.*?)\"", item) or \
                        re.search(r"<link[^>]*>(.*?)</link>", item, re.S)
                    if not t or not l:
                        continue
                    out.append(Candidate(
                        title=_unxml(t.group(1)),
                        url=_unxml(l.group(1)),
                        source=f"rss:{label}",
                    ))
            LOG.info("rss %s -> %d items", label, len(out) - before)
        except Exception as exc:  # noqa: BLE001 — one bad feed shouldn't kill the run
            LOG.warning("rss %s failed: %s", label, exc)
    return out


def _unxml(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(a, b)
    return s.strip()


YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


def resolve_channel_id(spec: str) -> str | None:
    """
    Turn whatever the user put in config into a channel_id.

    Accepts a bare UC… id, a full feed URL, a /channel/UC… URL, or an @handle
    (which needs one page fetch, since the handle is not the id). Returns None
    if it can't be resolved — a bad entry warns rather than killing the run.
    """
    spec = spec.strip()
    if not spec:
        return None
    if spec.startswith("UC") and len(spec) > 20:
        return spec

    m = re.search(r"channel_id=(UC[\w-]+)", spec) or re.search(r"/channel/(UC[\w-]+)", spec)
    if m:
        return m.group(1)

    handle = spec if spec.startswith("http") else f"https://www.youtube.com/{spec.lstrip('/')}"
    try:
        html = _http_get(handle, timeout=20)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("youtube channel %r could not be fetched (%s) — check the "
                    "handle exists, or paste the UC… id from the channel's "
                    "About page instead", spec, exc)
        return None

    # Order matters. A channel page mentions many "channelId" values — every
    # recommendation shelf and featured channel has one — so matching that
    # first silently resolves to somebody else's channel. The canonical link
    # and externalId both refer to the page's own channel.
    patterns = [
        r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]+)"',
        r'"externalId":"(UC[\w-]+)"',
        r'<meta itemprop="identifier" content="(UC[\w-]+)"',
        r'"channelId":"(UC[\w-]+)"',          # last resort, unreliable
    ]
    for i, pat in enumerate(patterns):
        m = re.search(pat, html)
        if m:
            if i == len(patterns) - 1:
                LOG.warning("%s resolved only via a weak match — verify with "
                            "--check-sources that it's the right channel", spec)
            LOG.info("resolved %s -> %s (paste this id into config to skip "
                     "the lookup)", spec, m.group(1))
            return m.group(1)

    LOG.warning("youtube channel %r: no channel id found on the page", spec)
    return None


def channel_specs(raw: list[Any]) -> list[tuple[str, str]]:
    """
    Normalise discovery.youtube_channels into (spec, declared_licence) pairs.

    Accepts both the plain form ("@NASA") and the declared form
    ({"channel": "UC…", "licence": "public-domain"}), so a config written
    either way keeps working.
    """
    out: list[tuple[str, str]] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            spec = (entry.get("channel") or "").strip()
            lic = entry.get("licence", "")
            lic = lic if lic in ("public-domain", "cc") else ""
        else:
            spec, lic = str(entry).strip(), ""
        if spec:
            out.append((spec, lic))
    return out


def discover_youtube_rss(
    channels: list[str] | list[tuple[str, str]],
    per_channel: int = 8,
    max_age_hours: int = 0,
) -> list[Candidate]:
    """
    Latest uploads from chosen channels, via YouTube's public RSS feed.

    No API key and no quota cost — unlike search.list, which bills 100 units
    against the same daily budget the uploads themselves draw from. The
    trade-off is that this is not "trending": it is only what these channels
    have published, which is also what makes the copyright position defensible.
    """
    out: list[Candidate] = []
    cutoff = None
    if max_age_hours:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)

    for item in channels:
        # Each entry is either "@handle"/"UC…" or ("@handle"/"UC…", licence).
        # Carrying the licence on the Candidate — rather than matching the
        # channel's display name later — is what lets you paste a raw UC id
        # into config without silently losing that channel's trusted status.
        spec, declared = item if isinstance(item, tuple) else (item, "")
        cid = resolve_channel_id(spec)
        if not cid:
            continue
        before = len(out)
        try:
            xml = _http_get(YT_FEED.format(cid))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("youtube feed for %s failed: %s", spec, exc)
            continue

        entries = re.findall(r"<entry>.*?</entry>", xml, re.S)
        if not entries:
            LOG.warning("youtube feed for %s (%s) has no entries at all — that "
                        "id is probably not the channel you meant", spec, cid)

        for entry in entries[:per_channel]:
            vid = re.search(r"<yt:videoId>(.*?)</yt:videoId>", entry)
            title = re.search(r"<title>(.*?)</title>", entry, re.S)
            published = re.search(r"<published>(.*?)</published>", entry)
            desc = re.search(r"<media:description>(.*?)</media:description>", entry, re.S)
            author = re.search(r"<name>(.*?)</name>", entry, re.S)
            if not vid or not title:
                continue

            if cutoff and published:
                try:
                    when = dt.datetime.fromisoformat(published.group(1).replace("Z", "+00:00"))
                    if when < cutoff:
                        continue
                except ValueError:
                    pass

            url = f"https://www.youtube.com/watch?v={vid.group(1)}"
            out.append(Candidate(
                title=_unxml(title.group(1)),
                url=url,
                video_url=url,
                source=f"youtube:{_unxml(author.group(1)) if author else spec}",
                summary=_unxml(desc.group(1))[:400] if desc else "",
                published=published.group(1) if published else "",
                licence=declared,
            ))
        kept = len(out) - before
        if cutoff and kept < len(entries[:per_channel]):
            LOG.info("youtube %s -> %d videos (%d in feed, rest older than "
                     "max_age_hours)", spec, kept, len(entries))
        else:
            LOG.info("youtube %s -> %d videos", spec, kept)
    return out


def check_music(config: dict[str, Any]) -> int:
    """Report what the music library actually contains, mood by mood."""
    audio = config.get("audio", {})
    base = Path(audio.get("library_dir", "assets/music"))
    if not base.is_absolute():
        base = ROOT / base
    moods = audio.get("moods") or ["neutral"]
    exts = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus")

    print(f"Music library: {base}\n")
    empty = []
    for mood in moods:
        folder = base / mood
        tracks = sorted(t.name for t in folder.iterdir()
                        if t.suffix.lower() in exts and not t.name.startswith(".")
                        ) if folder.is_dir() else []
        mark = "✓" if tracks else "·"
        print(f"  {mark} {mood:<10} {len(tracks)} track(s)")
        for t in tracks[:4]:
            print(f"        {t}")
        if len(tracks) > 4:
            print(f"        … and {len(tracks) - 4} more")
        if not tracks:
            empty.append(mood)

    print()
    if "neutral" in empty:
        print("!! 'neutral' is EMPTY. It is the fallback for every mood that has no\n"
              "   tracks, so with it empty a run renders silently. Fill it first.")
    elif empty:
        print(f"Moods with no tracks (they fall back to neutral): {', '.join(empty)}")
    else:
        print("Every mood has at least one track.")
    print("\nFree sources: YouTube Audio Library (studio.youtube.com > Audio library —\n"
          "safest, and it has a Mood filter), Pixabay Music, Free Music Archive.")
    return 0


def check_sources(config: dict[str, Any]) -> int:
    """
    Resolve every configured channel and show what it actually returns.

    Curating the channel list is the editorial work of this project, and doing
    it blind is miserable — this prints the feed's OWN title so you can see at
    a glance whether an @handle resolved to the channel you meant.
    """
    d = config.get("discovery", {})
    channels = channel_specs(d.get("youtube_channels", []))
    if not channels:
        print("No youtube_channels configured.")
        return 1

    print(f"Checking {len(channels)} channel(s)…\n")
    problems = 0
    for spec, declared in channels:
        cid = resolve_channel_id(spec)
        if not cid:
            print(f"  ✗ {spec:<28} could not resolve")
            problems += 1
            continue
        try:
            xml = _http_get(YT_FEED.format(cid))
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {spec:<28} {cid}  feed error: {exc}")
            problems += 1
            continue

        name = re.search(r"<title>(.*?)</title>", xml, re.S)
        entries = re.findall(r"<entry>.*?</entry>", xml, re.S)
        newest = re.search(r"<title>(.*?)</title>", entries[0], re.S) if entries else None
        label = _unxml(name.group(1)) if name else "?"
        if not entries:
            print(f"  ✗ {spec:<28} {cid}  '{label}' — NO VIDEOS")
            problems += 1
        else:
            tag = f"  [{declared}]" if declared else "  [licence verified per-video]"
            print(f"  ✓ {spec:<28} {cid}  '{label}' — {len(entries)} videos{tag}")
            if newest:
                print(f"      newest: {_unxml(newest.group(1))[:70]}")
    print()
    if problems:
        print(f"{problems} channel(s) need attention. Fix: open the channel in a "
              "browser, click About → Share → Copy channel ID, and paste the "
              "UC… id into config.json.")
    return 0


WIKI_FEED = "https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{}/{}/{}"


def _wiki_image(page: dict[str, Any], min_width: int = 600) -> str:
    """Prefer the original file; fall back to the thumbnail."""
    for key in ("originalimage", "thumbnail"):
        img = page.get(key) or {}
        src = img.get("source")
        if src and int(img.get("width") or 0) >= (min_width if key == "thumbnail" else 0):
            return src
    return ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def discover_wikimedia(
    sections: list[str],
    days_back: int = 2,
    max_per_section: int = 6,
) -> list[Candidate]:
    """
    Story candidates from Wikimedia's featured feed.

    One request per day returns the picture of the day, on-this-day events and
    the most-read articles — all of it explicitly licensed for reuse, which is
    the whole reason we are here rather than on Reddit. Attribution still
    matters for CC-BY material, so every candidate carries a credit line that
    ends up in the video description.
    """
    out: list[Candidate] = []
    today = dt.datetime.now(dt.timezone.utc).date()

    for delta in range(days_back):
        day = today - dt.timedelta(days=delta)
        url = WIKI_FEED.format(day.year, f"{day.month:02d}", f"{day.day:02d}")
        if delta:
            time.sleep(1.0)  # space consecutive days — this endpoint rate-limits
        try:
            data = json.loads(_http_get(url, timeout=25))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("wikimedia feed %s failed: %s", day, exc)
            continue

        # ---- picture of the day -------------------------------------------
        if "image" in sections and isinstance(data.get("image"), dict):
            img = data["image"]
            src = (img.get("original") or {}).get("source") or ""
            desc = _clean(((img.get("description") or {}).get("text")) or "")
            artist = _clean(((img.get("artist") or {}).get("text")) or "")
            lic = (img.get("license") or {}).get("type") or ""
            if src and desc:
                out.append(Candidate(
                    title=desc[:300],
                    url=img.get("file_page") or src,
                    source="wikimedia:picture-of-the-day",
                    kind="story",
                    images=[src],
                    summary=desc[:600],
                    commentary="",
                    attribution=" / ".join(x for x in (artist, lic) if x),
                    published=day.isoformat(),
                    score=500,
                ))

        # ---- on this day ---------------------------------------------------
        if "onthisday" in sections:
            for ev in (data.get("onthisday") or [])[:max_per_section]:
                pages = [p for p in (ev.get("pages") or []) if _wiki_image(p)]
                if not pages:
                    continue
                year = ev.get("year")
                text = _clean(ev.get("text") or "")
                if not text:
                    continue
                page = pages[0]
                out.append(Candidate(
                    title=(f"On this day in {year}: {text}" if year else text)[:300],
                    url=(page.get("content_urls") or {}).get("desktop", {}).get("page", ""),
                    source="wikimedia:on-this-day",
                    kind="story",
                    images=[_wiki_image(p) for p in pages[:2]],
                    summary=_clean(page.get("extract") or "")[:600],
                    attribution="Wikimedia Commons",
                    published=day.isoformat(),
                    score=300,
                ))

        # ---- most read ------------------------------------------------------
        if "mostread" in sections:
            for page in ((data.get("mostread") or {}).get("articles") or [])[:max_per_section]:
                src = _wiki_image(page)
                extract = _clean(page.get("extract") or "")
                if not src or len(extract) < 80:
                    continue
                out.append(Candidate(
                    title=extract[:300],
                    url=(page.get("content_urls") or {}).get("desktop", {}).get("page", ""),
                    source="wikimedia:most-read",
                    kind="story",
                    images=[src],
                    summary=extract[:600],
                    attribution="Wikimedia Commons",
                    published=day.isoformat(),
                    score=int(page.get("views") or 0) // 1000,
                ))

        LOG.info("wikimedia %s -> %d story candidates", day, len(out))
        time.sleep(0.5)
    return out


WIKI_API = "https://en.wikipedia.org/w/api.php"


def is_free_image_url(url: str) -> bool:
    """
    Free-licence check with no extra request.

    Wikimedia serves free files from /wikipedia/commons/ — Commons only
    accepts freely-licensed material. Non-free files (film posters, album
    covers, game box art, promotional stills) are uploaded *locally* to a
    language wiki under a fair-use rationale that covers Wikipedia's own use
    and nobody else's, and they serve from /wikipedia/en/ instead.

    So the path segment alone separates "reusable" from "will get you a
    copyright claim", for free, on every image.
    """
    return "/wikipedia/commons/" in url


def discover_wikipedia_search(
    queries: list[str],
    per_query: int = 8,
    free_only: bool = True,
) -> list[Candidate]:
    """
    Topic-driven story candidates: search Wikipedia, keep articles with images.

    Search rather than categories on purpose — category names are easy to get
    subtly wrong and fail silently, whereas a free-text query either returns
    articles or obviously doesn't.
    """
    from urllib.parse import urlencode

    out: list[Candidate] = []
    for q in queries:
        before = len(out)
        params = urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "generator": "search", "gsrsearch": q, "gsrlimit": per_query,
            "gsrnamespace": "0",
            "prop": "extracts|pageimages|info", "inprop": "url",
            "exintro": "1", "explaintext": "1", "exsentences": "3",
            "piprop": "original|thumbnail", "pithumbsize": "1200",
        })
        try:
            data = json.loads(_http_get(f"{WIKI_API}?{params}", timeout=25))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("wikipedia search %r failed: %s", q, exc)
            continue

        skipped_nonfree = 0
        for page in (data.get("query", {}) or {}).get("pages", []):
            img = (page.get("original") or page.get("thumbnail") or {}).get("source", "")
            extract = _clean(page.get("extract") or "")
            if not img or len(extract) < 80:
                continue
            if free_only and not is_free_image_url(img):
                skipped_nonfree += 1
                continue
            out.append(Candidate(
                title=extract[:300],
                url=page.get("fullurl") or f"https://en.wikipedia.org/wiki/{page.get('title','')}",
                source=f"wikipedia:{q[:28]}",
                kind="story",
                images=[img],
                summary=extract[:600],
                attribution="Wikimedia Commons" if is_free_image_url(img) else "Wikipedia",
                score=200,
            ))
        msg = f"wikipedia '{q[:34]}' -> {len(out) - before} candidates"
        if skipped_nonfree:
            msg += f" ({skipped_nonfree} dropped: non-free image)"
        LOG.info(msg)
        time.sleep(0.4)
    return out


_REDDIT_TOKEN: str | None = None


def reddit_token() -> str | None:
    """
    Get an application-only OAuth token for a Reddit 'script' app.

    Uses the client_credentials grant, which needs only the app's id and
    secret — NOT the account password. That is enough for reading public
    listings, and it means no password ever sits in an env file.
    """
    global _REDDIT_TOKEN
    if _REDDIT_TOKEN:
        return _REDDIT_TOKEN

    cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not cid or not secret or "REPLACE_ME" in (cid + secret):
        LOG.info("reddit: no credentials configured — skipping reddit discovery")
        return None

    import base64
    import urllib.error
    import urllib.parse
    import urllib.request

    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _ssl_context()

    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "User-Agent": reddit_user_agent(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
            _REDDIT_TOKEN = json.loads(resp.read()).get("access_token")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        LOG.error("reddit auth failed (%s): %s — check REDDIT_CLIENT_ID/SECRET "
                  "and that the app type is 'script'", exc.code, body)
        return None
    except Exception as exc:  # noqa: BLE001
        LOG.error("reddit auth failed: %s", exc)
        return None

    if _REDDIT_TOKEN:
        LOG.info("reddit: authenticated")
    return _REDDIT_TOKEN


def reddit_user_agent() -> str:
    # Reddit rate-limits or blocks generic agents outright, and asks for
    # platform:appid:version (by /u/username).
    return os.environ.get(
        "REDDIT_USER_AGENT", "python:miscellaneous-ken:1.0 (by /u/unknown)"
    )


def reddit_get(path: str, token: str, timeout: int = 20) -> dict[str, Any]:
    """GET from oauth.reddit.com — the authenticated host, not www."""
    import urllib.request

    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _ssl_context()

    req = urllib.request.Request(
        f"https://oauth.reddit.com{path}",
        headers={"Authorization": f"bearer {token}", "User-Agent": reddit_user_agent()},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _post_images(p: dict[str, Any], max_images: int) -> list[str]:
    """
    Pull image URLs out of a post: galleries first, then a direct image link.

    Galleries are what make the side-by-side comparison format work, so they
    are preferred over the single preview image.
    """
    urls: list[str] = []

    if p.get("is_gallery") and isinstance(p.get("media_metadata"), dict):
        order = [i.get("media_id") for i in (p.get("gallery_data") or {}).get("items", [])]
        for mid in order:
            meta = p["media_metadata"].get(mid) or {}
            best = (meta.get("s") or {}).get("u") or (meta.get("s") or {}).get("gif")
            if best:
                urls.append(best.replace("&amp;", "&"))
            if len(urls) >= max_images:
                break
        if urls:
            return urls

    direct = p.get("url_overridden_by_dest") or p.get("url") or ""
    if direct.lower().split("?")[0].endswith(IMAGE_EXTS):
        urls.append(direct)
        return urls

    # Fall back to reddit's own preview render of a linked image.
    previews = (p.get("preview") or {}).get("images") or []
    for pv in previews[:max_images]:
        src = (pv.get("source") or {}).get("url")
        if src:
            urls.append(src.replace("&amp;", "&"))
    return urls


def reddit_top_comment(permalink: str, token: str, min_score: int = 50) -> str:
    """The top comment, used as the commentary line under the images."""
    try:
        data = reddit_get(f"{permalink}?limit=5&sort=top&depth=1", token)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("could not fetch comments for %s: %s", permalink, exc)
        return ""
    if not isinstance(data, list) or len(data) < 2:
        return ""
    for child in data[1].get("data", {}).get("children", []):
        c = child.get("data", {})
        if c.get("stickied") or c.get("distinguished"):
            continue
        body = (c.get("body") or "").strip()
        if body and c.get("score", 0) >= min_score and 20 <= len(body) <= 280:
            return body
    return ""


def fetch_reddit_json(
    sub: str,
    sort: str = "hot",
    limit: int = 5,
    token: str | None = None,
    retries: int = 3,
) -> list[dict[str, Any]]:
    """Fetch subreddit posts using OAuth if token is available, or public JSON endpoint, with rate-limiting."""
    headers = {"User-Agent": reddit_user_agent()}
    if token:
        url = f"https://oauth.reddit.com/r/{sub}/{sort}?limit={limit}"
        headers["Authorization"] = f"bearer {token}"
    else:
        url = f"https://www.reddit.com/r/{sub}/{sort}.json?limit={limit}"

    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            remaining = float(resp.headers.get("x-ratelimit-remaining", 10))
            reset_sec = float(resp.headers.get("x-ratelimit-reset", 1))

            if resp.status_code == 200:
                payload = resp.json()
                delay = (reset_sec / remaining) if (remaining > 0 and remaining < 20) else 1.5
                time.sleep(delay)
                return payload.get("data", {}).get("children", [])
            elif resp.status_code == 429:
                wait_time = max(reset_sec, float(2 ** (attempt + 1)))
                LOG.warning("[429 Rate Limited] r/%s sleeping for %.1fs (attempt %d/%d)", sub, wait_time, attempt + 1, retries)
                time.sleep(wait_time)
            elif resp.status_code in (403, 404):
                if resp.status_code == 403 and not token:
                    LOG.info("[Skipped] r/%s 403 (unauthenticated blocked by Reddit network policy; configure REDDIT_CLIENT_ID/SECRET in mken.env)", sub)
                else:
                    LOG.info("[Skipped] r/%s (HTTP %d)", sub, resp.status_code)
                return []
            else:
                LOG.warning("r/%s HTTP %d: %s", sub, resp.status_code, resp.text[:120])
                time.sleep(1.5)
        except requests.RequestException as err:
            LOG.warning("r/%s connection error: %s (attempt %d/%d)", sub, err, attempt + 1, retries)
            time.sleep(2)

    return []


def discover_reddit_categorized(
    categories: dict[str, list[str]],
    state: State | None = None,
    rotate_per_cat: int = 1,
    limit: int = 5,
    min_score: int = 500,
    story_min_score: int = 2000,
    sort: str = "hot",
    max_images: int = 2,
    with_comment: bool = True,
) -> list[Candidate]:
    """
    Discover both video clips and image stories from categorized subreddits.
    Rotates through subreddits per category using State to pace API usage.
    """
    out: list[Candidate] = []
    token = reddit_token()

    for category, subs in categories.items():
        if not subs:
            continue

        if rotate_per_cat > 0 and len(subs) > rotate_per_cat:
            cursor = state.get_reddit_cursor(category) if state else 0
            selected_subs = [subs[(cursor + i) % len(subs)] for i in range(rotate_per_cat)]
            if state:
                state.set_reddit_cursor(category, (cursor + rotate_per_cat) % len(subs))
        else:
            selected_subs = subs

        for sub in selected_subs:
            sub = re.sub(r"^/?r/", "", sub).strip("/")
            before = len(out)
            raw_posts = fetch_reddit_json(sub, sort=sort, limit=limit, token=token)

            for child in raw_posts:
                p = child.get("data", {})
                if p.get("stickied") or p.get("over_18"):
                    continue
                score = int(p.get("score", 0))
                permalink = f"https://www.reddit.com{p.get('permalink', '')}"
                title = (p.get("title") or "").strip()
                selftext = (p.get("selftext") or "").strip()
                published = dt.datetime.fromtimestamp(
                    p.get("created_utc", 0), dt.timezone.utc
                ).isoformat()

                # Check for video
                video_url = ""
                if p.get("is_video") and p.get("media", {}).get("reddit_video"):
                    video_url = permalink
                else:
                    ext = (p.get("url_overridden_by_dest") or p.get("url") or "")
                    if any(d in ext for d in ("youtube.com", "youtu.be", "v.redd.it", "streamable.com")):
                        video_url = ext

                if video_url and score >= min_score:
                    out.append(Candidate(
                        title=title,
                        url=permalink,
                        video_url=video_url,
                        source=f"reddit:{category}:r/{sub}",
                        kind="video",
                        score=score,
                        summary=selftext[:400],
                        published=published,
                    ))
                    continue

                # Check for story (images / gallery)
                images = _post_images(p, max_images)
                if images and score >= story_min_score:
                    commentary = selftext[:280]
                    if not commentary and with_comment and token:
                        commentary = reddit_top_comment(p.get("permalink", ""), token)
                    out.append(Candidate(
                        title=title,
                        url=permalink,
                        source=f"reddit:{category}:r/{sub}",
                        kind="story",
                        images=images,
                        commentary=commentary,
                        score=score,
                        summary=selftext[:400],
                        published=published,
                    ))

            added = len(out) - before
            LOG.info("reddit [%s] r/%s -> %d candidate(s)", category, sub, added)

    return out


def discover_reddit_stories(
    subreddits: list[str],
    limit: int = 25,
    min_score: int = 800,
    max_images: int = 2,
    with_comment: bool = True,
) -> list[Candidate]:
    """Image posts from Reddit, for the story-card format."""
    token = reddit_token()
    out: list[Candidate] = []
    for sub in subreddits:
        sub = re.sub(r"^/?r/", "", sub).strip("/")
        before = len(out)
        raw_posts = fetch_reddit_json(sub, sort="hot", limit=limit, token=token)

        for child in raw_posts:
            p = child.get("data", {})
            if p.get("stickied") or p.get("over_18") or p.get("is_video"):
                continue
            if int(p.get("score", 0)) < min_score:
                continue
            images = _post_images(p, max_images)
            if not images:
                continue

            permalink = p.get("permalink", "")
            out.append(Candidate(
                title=(p.get("title") or "").strip(),
                url=f"https://www.reddit.com{permalink}",
                source=f"reddit:r/{sub}",
                kind="story",
                images=images,
                commentary=reddit_top_comment(permalink, token) if (with_comment and token) else (p.get("selftext") or "")[:280],
                score=int(p.get("score", 0)),
                summary=(p.get("selftext") or "")[:400],
                published=dt.datetime.fromtimestamp(
                    p.get("created_utc", 0), dt.timezone.utc).isoformat(),
            ))
        LOG.info("reddit r/%s -> %d story candidates", sub, len(out) - before)
    return out


def discover_reddit(subreddits: list[str], limit: int = 15, min_score: int = 500) -> list[Candidate]:
    """Hot posts via the Reddit endpoint. Video posts only."""
    token = reddit_token()
    out: list[Candidate] = []
    for sub in subreddits:
        sub = re.sub(r"^/?r/", "", sub).strip("/")
        before = len(out)
        raw_posts = fetch_reddit_json(sub, sort="hot", limit=limit, token=token)

        for child in raw_posts:
            p = child.get("data", {})
            if p.get("stickied") or p.get("over_18"):
                continue
            score = int(p.get("score", 0))
            if score < min_score:
                continue

            video_url = ""
            if p.get("is_video") and p.get("media", {}).get("reddit_video"):
                video_url = f"https://www.reddit.com{p.get('permalink')}"
            else:
                ext = (p.get("url_overridden_by_dest") or p.get("url") or "")
                if any(d in ext for d in ("youtube.com", "youtu.be", "v.redd.it", "streamable.com")):
                    video_url = ext
            if not video_url:
                continue

            out.append(Candidate(
                title=(p.get("title") or "").strip(),
                url=f"https://www.reddit.com{p.get('permalink')}",
                video_url=video_url,
                source=f"reddit:r/{sub}",
                score=score,
                summary=(p.get("selftext") or "")[:400],
                published=dt.datetime.fromtimestamp(
                    p.get("created_utc", 0), dt.timezone.utc
                ).isoformat(),
            ))
        LOG.info("reddit r/%s -> %d video candidates", sub, len(out) - before)
    return out



def _video_id(url: str) -> str:
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


def verify_youtube_licenses(
    cands: list[Candidate],
    api_key: str,
    trusted: dict[str, str] | None = None,
    max_seconds: int = 0,
) -> list[Candidate]:
    """
    Keep only clips we are actually allowed to repost.

    This is the check that lets discovery run unattended. RSS feeds carry no
    licence field, so without it the agent cannot tell a Creative Commons
    upload from an all-rights-reserved one — and a bot that reposts the wrong
    one collects strikes, not views.

    videos.list costs ONE quota unit per call and takes 50 ids at a time, so
    verifying a whole run is rounding error against the 1,600 an upload costs.

    Two ways a clip passes:
      * status.license == "creativeCommon" — YouTube's own CC-BY flag, set by
        the uploader, which is a licence grant to reuse with credit.
      * its channel is named in discovery.youtube_channels with an explicit
        licence of "public-domain" or "cc". Needed because US government work
        (NASA, NOAA) is public domain by statute but still shows the default
        "youtube" licence, so the API alone would wrongly reject it.

    Same call returns duration and liveStreamingDetails, so unstarted
    premieres and livestreams — which used to reach the renderer and fail —
    are dropped here instead.
    """
    if not cands:
        return []

    def _is_trusted(c: Candidate) -> bool:
        # Set at discovery from the channel's declared licence in config.
        return c.licence in ("public-domain", "cc")

    yt = [c for c in cands if _video_id(c.video_url)]
    other = [c for c in cands if not _video_id(c.video_url)]
    if not yt:
        return cands

    if not api_key:
        kept = [c for c in yt if _is_trusted(c)]
        LOG.warning(
            "no YOUTUBE_API_KEY — cannot read licence flags, so only the %d "
            "clip(s) from channels you marked public-domain/cc in config are "
            "usable (%d dropped). Set YOUTUBE_API_KEY to open this up.",
            len(kept), len(yt) - len(kept))
        return other + kept

    from urllib.parse import urlencode
    by_id = {_video_id(c.video_url): c for c in yt}
    ids = list(by_id)
    kept: list[Candidate] = []
    reasons: dict[str, int] = {}

    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        params = urlencode({
            "part": "status,contentDetails,liveStreamingDetails,snippet",
            "id": ",".join(batch), "key": api_key,
        })
        try:
            data = json.loads(_http_get(
                f"https://www.googleapis.com/youtube/v3/videos?{params}"))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("licence check failed (%s) — dropping %d clip(s) rather "
                        "than guessing", exc, len(batch))
            reasons["licence check failed"] = reasons.get("licence check failed", 0) + len(batch)
            continue

        for item in data.get("items", []):
            c = by_id.get(item.get("id", ""))
            if not c:
                continue
            st = item.get("status", {})
            cd = item.get("contentDetails", {})
            live = item.get("snippet", {}).get("liveBroadcastContent", "none")

            def drop(why: str) -> None:
                reasons[why] = reasons.get(why, 0) + 1

            if live and live != "none":
                drop(f"live/upcoming ({live})")
                continue
            if item.get("liveStreamingDetails") and not \
                    item["liveStreamingDetails"].get("actualEndTime"):
                drop("livestream not finished")
                continue
            if st.get("privacyStatus") != "public":
                drop("not public")
                continue
            if cd.get("regionRestriction", {}).get("blocked"):
                drop("region blocked")
                continue

            secs = _iso8601_seconds(cd.get("duration", ""))
            if max_seconds and secs and secs > max_seconds:
                drop(f"longer than {max_seconds}s")
                continue
            if secs == 0:
                drop("zero/unknown duration")
                continue

            lic = st.get("license", "youtube")
            if lic == "creativeCommon":
                c.attribution = (f"{item.get('snippet', {}).get('channelTitle', '')} "
                                 f"(CC BY, via YouTube)").strip()
                kept.append(c)
            elif _is_trusted(c):
                c.attribution = item.get("snippet", {}).get("channelTitle", "")
                kept.append(c)
            else:
                drop("all rights reserved")

    if reasons:
        LOG.info("licence check dropped %d clip(s): %s",
                 sum(reasons.values()),
                 ", ".join(f"{n}x {w}" for w, n in sorted(
                     reasons.items(), key=lambda kv: -kv[1])))
    LOG.info("licence check kept %d of %d clip(s) (cost: %d quota unit(s))",
             len(kept), len(yt), (len(ids) + 49) // 50)
    return other + kept


def _iso8601_seconds(dur: str) -> int:
    m = re.fullmatch(r"P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", dur or "")
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def discover_youtube(queries: list[str], api_key: str, per_query: int = 8,
                     days_back: int = 0, cc_only: bool = True) -> list[Candidate]:
    """
    YouTube Data API search — the autonomous clip source.

    videoLicense=creativeCommon makes YouTube do the filtering server-side, so
    the results are reusable by construction rather than by hope. That is what
    lets this run without anyone hand-picking a URL. It costs 100 units per
    query against the 10,000/day budget, so keep the query list short.

    days_back=0 searches all time, which is what you want for subject matter
    ("how a synthesiser works") as opposed to news.
    """
    if not api_key or not queries:
        if queries and not api_key:
            LOG.warning("youtube_queries are set but YOUTUBE_API_KEY is not — "
                        "skipping the search-based clip source entirely")
        return []
    out: list[Candidate] = []

    for q in queries:
        from urllib.parse import urlencode
        params: dict[str, Any] = {
            "part": "snippet", "q": q, "type": "video", "order": "viewCount",
            "maxResults": per_query,
            "videoDuration": "short", "videoEmbeddable": "true", "key": api_key,
        }
        if cc_only:
            params["videoLicense"] = "creativeCommon"
        if days_back:
            params["publishedAfter"] = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = json.loads(_http_get(
                f"https://www.googleapis.com/youtube/v3/search?{urlencode(params)}"))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("youtube search %r failed: %s", q, exc)
            continue
        before = len(out)
        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            sn = item.get("snippet", {})
            if not vid:
                continue
            out.append(Candidate(
                title=_unxml(sn.get("title", "")),
                url=f"https://www.youtube.com/watch?v={vid}",
                video_url=f"https://www.youtube.com/watch?v={vid}",
                source=f"youtube:{sn.get('channelTitle', q)}",
                summary=sn.get("description", "")[:400],
                published=sn.get("publishedAt", ""),
                attribution=f"{sn.get('channelTitle', '')} (CC BY, via YouTube)".strip(),
            ))
        LOG.info("youtube search %r -> %d clip(s)%s", q, len(out) - before,
                 " [CC only]" if cc_only else "")
    return out


def discover(config: dict[str, Any], state: State | None = None) -> tuple[list[Candidate], list[str]]:
    """
    Returns (candidates, context_headlines).

    Candidates are postable: each has a clip yt-dlp can fetch. Context
    headlines come from the news feeds — they inform Claude's sense of what
    today looks like, but they cannot be picked, because there is no video
    behind a news article URL.
    """
    d = config.get("discovery", {})
    cands: list[Candidate] = []

    # youtube_channels entries are either "@handle" (licence unknown — every
    # clip must pass the API check) or {"channel": "@handle", "licence": "..."}
    # where licence is public-domain / cc / verify.
    cands += discover_youtube_rss(
        channel_specs(d.get("youtube_channels", [])),
        d.get("per_channel", 8),
        d.get("max_age_hours", 0),
    )
    cands += discover_wikipedia_search(
        d.get("wikipedia_queries", []),
        int(d.get("per_query", 8)),
        bool(d.get("free_images_only", True)),
    )
    cands += discover_wikimedia(
        d.get("wikimedia_sections", ["image", "onthisday", "mostread"]),
        int(d.get("wikimedia_days_back", 2)),
    )

    # Reddit categorized discovery (videos + stories)
    reddit_cats = d.get("reddit_categories", {})
    if reddit_cats:
        cands += discover_reddit_categorized(
            categories=reddit_cats,
            state=state,
            rotate_per_cat=int(d.get("reddit_rotate_per_category", 1)),
            limit=int(d.get("reddit_posts_per_sub", d.get("reddit_limit", 5))),
            min_score=int(d.get("reddit_min_score", 500)),
            story_min_score=int(d.get("story_min_score", 2000)),
            sort=str(d.get("reddit_sort", "hot")),
            max_images=int((config.get("story", {}) or {}).get("max_images", 2)),
        )

    if d.get("story_subreddits"):
        cands += discover_reddit_stories(
            d.get("story_subreddits", []),
            d.get("reddit_limit", 25),
            d.get("story_min_score", 2000),
            int((config.get("story", {}) or {}).get("max_images", 2)),
        )
    if d.get("subreddits"):
        cands += discover_reddit(
            d.get("subreddits", []),
            d.get("reddit_limit", 15),
            d.get("reddit_min_score", 500),
        )

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    cands += discover_youtube(
        d.get("youtube_queries", []),
        api_key,
        d.get("youtube_per_query", 8),
        int(d.get("youtube_days_back", 0)),
        bool(d.get("youtube_cc_only", True)),
    )

    # Everything above this line is a *claim* that a clip exists. This is where
    # we find out whether we may actually use it.
    if d.get("license_check", True):
        cands = verify_youtube_licenses(
            cands, api_key, None,
            # The source may be far longer than the 30s we keep — we trim it.
            # This only rejects the pathological case (a 3-hour upload) that
            # would spend ten minutes downloading for a few usable seconds.
            int(d.get("max_source_seconds", 900)),
        )

    # Drop anything with no title, and de-dupe within the run.
    seen: set[str] = set()
    uniq: list[Candidate] = []
    for c in cands:
        if not c.title or c.key in seen:
            continue
        seen.add(c.key)
        uniq.append(c)

    # A post without a clip is not a post. RSS gives us headlines, not video:
    # an article URL has nothing for yt-dlp to download, so without this filter
    # those candidates burn a Claude call and then fail at the download step.
    if d.get("require_video", True):
        # Story candidates carry images instead of a clip — they must survive
        # this filter, which exists only to catch headline-only RSS items.
        with_video = [c for c in uniq
                      if c.video_url or (c.kind == "story" and c.images)]
        dropped = len(uniq) - len(with_video)
        if dropped:
            LOG.info("dropped %d candidate(s) with no video source "
                     "(discovery.require_video)", dropped)
        if not with_video and uniq:
            LOG.warning(
                "every candidate was headline-only — no video source is "
                "working. Check that Reddit/YouTube discovery is configured; "
                "RSS feeds alone cannot supply clips."
            )
        uniq = with_video

    # News headlines: context for the editor, never candidates.
    context: list[str] = []
    news_feeds = d.get("news_rss") or d.get("rss_feeds") or []
    if news_feeds:
        for c in discover_rss(news_feeds, d.get("per_feed", 10)):
            context.append(f"[{c.source.removeprefix('rss:')}] {c.title}")

    return uniq, context


# ==========================================================================
# Editorial pick (Claude)
# ==========================================================================

# --------------------------------------------------------------------------
# Providers
#
# The editorial step is one structured call: "here are ~100 candidates, pick
# the best few and write a headline". Three vendors can do that, so the agent
# tries them in order and uses the first that has a usable key. That is not
# vendor-neutrality for its own sake — it is what stops an empty balance at
# one vendor from taking the channel off the air, which is exactly what
# happened here.
#
# Order comes from editorial.providers. Gemini is worth having in the list
# even if you prefer another: its flash models have a free tier.
# --------------------------------------------------------------------------

PROVIDER_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY", "sk-ant-"),
    "gemini": ("GEMINI_API_KEY", ""),
    "openai": ("OPENAI_API_KEY", "sk-"),
}

DEFAULT_MODELS = {
    # Verified against each vendor's own docs in September 2026. Model names
    # rot fast — override in config.json rather than editing this.
    "anthropic": "claude-sonnet-4-5",
    "gemini": "gemini-3.8-flash",     # free tier
    "openai": "gpt-5.6-luna",
}


def provider_key(name: str) -> str:
    """The configured key for a provider, or '' if it is absent/placeholder."""
    env, prefix = PROVIDER_ENV.get(name, ("", ""))
    key = os.environ.get(env, "").strip()
    if not key or "REPLACE_ME" in key or "..." in key or key in (prefix, "your-key-here"):
        return ""
    if prefix and not key.startswith(prefix):
        return ""
    return key


def check_api_key(key: str = "", config: dict[str, Any] | None = None) -> tuple[bool, str]:
    """
    Confirm at least one editorial provider is usable, before doing any work.

    Checked up front rather than at the API call: discovery takes ~30s and
    hammers every configured feed, and it is maddening to spend that only to
    fail on a key you never pasted. This only validates shape — an empty
    balance still fails at call time, which is why the fallback chain exists.
    """
    names = ((config or {}).get("editorial", {}).get("providers")
             or ["anthropic", "gemini", "openai"])
    usable = [n for n in names if provider_key(n)]
    if usable:
        LOG.debug("editorial providers with keys: %s", ", ".join(usable))
        return True, ""

    lines = ["No editorial provider has a usable API key. Set at least one:"]
    for n in names:
        env = PROVIDER_ENV.get(n, ("?", ""))[0]
        hint = {
            "anthropic": "console.anthropic.com → API keys (paid)",
            "gemini": "aistudio.google.com/api-keys (has a free tier)",
            "openai": "platform.openai.com → API keys (paid)",
        }.get(n, "")
        lines.append(f"  {env:<20} {hint}")
    lines.append("Put them in mken.env, or run with --no-llm to skip the "
                 "editorial step entirely.")
    return False, "\n".join(lines)


def _http_post_json(url: str, payload: dict[str, Any],
                    headers: dict[str, str], timeout: int = 120) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _ssl_context()
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        # The body carries the vendor's actual complaint ("model not found",
        # "quota exceeded", "unknown field"). Without it all you get is
        # "HTTP Error 400: Bad Request", which diagnoses nothing.
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc


def _find_picks(obj: Any, depth: int = 0) -> list[dict[str, Any]]:
    """
    Dig the picks array out of a provider response.

    Each vendor wraps the model's answer differently, and the envelopes change
    between API versions. Rather than hard-coding three response shapes that
    will rot, walk the structure for the payload we asked for: a dict with a
    "picks" list, or a string that parses into one.
    """
    if depth > 8:
        return []
    if isinstance(obj, dict):
        picks = obj.get("picks")
        if isinstance(picks, list) and all(isinstance(p, dict) for p in picks):
            return picks
        for v in obj.values():
            found = _find_picks(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_picks(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, str) and "picks" in obj:
        text = obj.strip()
        if text.startswith("```"):  # some models fence their JSON
            text = re.sub(r"^```[a-z]*\n|\n```$", "", text)
        try:
            return _find_picks(json.loads(text), depth + 1)
        except (ValueError, TypeError):
            return []
    return []


def _picks_schema(moods: list[str]) -> dict[str, Any]:
    """One JSON Schema, reused as an Anthropic tool, an OpenAI function and a
    Gemini response schema — they all speak the same subset."""
    return {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "candidate id"},
                        "headline": {"type": "string"},
                        "caption": {"type": "string"},
                        "mood": {
                            "type": "string",
                            "enum": moods,
                            "description": (
                                "Emotional register of the story. Picks the "
                                "background music, so choose by how the clip "
                                "should feel, not by topic."
                            ),
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "headline", "caption", "mood"],
                },
            }
        },
        "required": ["picks"],
    }


def _call_anthropic(system: str, user: str, schema: dict[str, Any],
                    model: str, key: str) -> list[dict[str, Any]]:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK missing: pip install anthropic") from None

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model, max_tokens=2000, system=system,
        tools=[{"name": "submit_picks",
                "description": "Submit the chosen stories with headline and caption.",
                "input_schema": schema}],
        tool_choice={"type": "tool", "name": "submit_picks"},
        messages=[{"role": "user", "content": user}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return list(block.input.get("picks", []))
    return []


def _call_openai(system: str, user: str, schema: dict[str, Any],
                 model: str, key: str) -> list[dict[str, Any]]:
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai SDK missing: pip install openai") from None

    client = openai.OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        tools=[{"type": "function",
                "function": {"name": "submit_picks",
                             "description": "Submit the chosen stories.",
                             "parameters": schema}}],
        tool_choice={"type": "function", "function": {"name": "submit_picks"}},
    )
    msg = resp.choices[0].message
    for call in (msg.tool_calls or []):
        found = _find_picks(call.function.arguments)
        if found:
            return found
    return _find_picks(msg.content or "")


def _call_gemini(system: str, user: str, schema: dict[str, Any],
                 model: str | list[str], key: str) -> list[dict[str, Any]]:
    """
    Gemini via generateContent — the endpoint that accepts a plain API key.

    Not /v1beta/interactions: that answers "Expected OAuth 2 access token" to
    an x-goog-api-key header, so it is unusable from a headless agent.

    `model` may be a list (or comma-separated string) and that matters. The
    free tier returns 503 UNAVAILABLE — "this model is experiencing high
    demand" — on the newest models regularly, and waiting does not help: the
    remedy for a busy model is a less fashionable model, not a longer sleep.
    So each model gets at most two quick attempts and then we move on.

    The second attempt drops responseSchema, because it is an OpenAPI subset
    rather than full JSON Schema and a rejected keyword comes back as a 400.
    The prompt already asks for the shape and _find_picks copes either way.
    """
    models = [m.strip() for m in
              (model if isinstance(model, list) else str(model).split(","))
              if str(m).strip()]
    last: Exception | None = None

    for name in models:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{name}:generateContent")
        base = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }
        shapes = [
            {**base, "generationConfig": {"responseMimeType": "application/json",
                                          "responseSchema": schema}},
            {**base, "generationConfig": {"responseMimeType": "application/json"}},
        ]
        for i, payload in enumerate(shapes):
            try:
                return _find_picks(_http_post_json(
                    url, payload, {"x-goog-api-key": key}))
            except RuntimeError as exc:
                last = exc
                text = str(exc)
                if "HTTP 400" in text and i == 0:
                    LOG.debug("gemini %s rejected the schema — retrying plain", name)
                    continue
                if "HTTP 503" in text or "HTTP 429" in text:
                    LOG.info("gemini %s is busy — trying the next model", name)
                    break
                if "HTTP 404" in text:
                    # This model does not exist for this key's API version.
                    # That says nothing about the others in the list, so it
                    # must not abort the provider — which is exactly the bug
                    # that stopped 2.5-flash's 404 from ever reaching 3.5.
                    LOG.info("gemini %s not available to this key — "
                             "trying the next model", name)
                    break
                # Bad key or revoked access: every other model would fail the
                # same way. Let the provider chain move on.
                raise
    raise last or RuntimeError("gemini: no response")


PROVIDER_CALLS = {
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
    "openai": _call_openai,
}


def pick_stories(
    candidates: list[Candidate],
    config: dict[str, Any],
    n: int,
    context: list[str] | None = None,
) -> list[Pick]:
    """Ask an LLM for the best N candidates with headline, caption and mood."""
    ed = config.get("editorial", {})
    max_headline = int(ed.get("max_headline_chars", 90))
    moods = available_moods(config)
    # An explicitly empty list means "no providers", not "use the defaults".
    providers = ed.get("providers")
    providers = ["anthropic", "gemini", "openai"] if providers is None else providers
    models = {**DEFAULT_MODELS, **(ed.get("models") or {})}
    if ed.get("model"):                       # legacy single-model config
        models["anthropic"] = ed["model"]

    system = ed.get("system_prompt") or (
        "You are the editor for a short-form news account called Miscellaneous Ken."
    )
    system += (
        f"\n\nHard rules:\n"
        f"- Headlines must be at most {max_headline} characters, plain text, no emoji, "
        f"no hashtags, no quotation marks. They are rendered in all-caps monospace, so "
        f"write them as normal sentences and let the renderer handle casing.\n"
        f"- Never invent facts. The headline must be supported by the candidate's title "
        f"and summary alone. If nothing is good enough, return fewer picks — or none.\n"
        f"- Skip anything tragic, graphic, medical-emergency, or involving named private "
        f"individuals in distress.\n"
        f"- Captions are 1-2 flat sentences of substance. Do NOT add a credit "
        f"or source line: the description already carries source, channel and "
        f"licence, and a second one just repeats it.\n"
        f"- Proofread the headline. A typo is rendered in large capitals and "
        f"cannot be edited after posting.\n"
        f"- Mood selects the backing track, so judge how the clip should feel, "
        f"not what it is about. Only use 'neutral' when a story genuinely has no "
        f"emotional colour — it is a last resort, not a safe default.\n"
        f"- Reply only with the submit_picks payload: an object with a 'picks' array."
    )

    listing = [
        {"id": i, "title": c.title, "source": c.source, "score": c.score,
         "summary": c.summary[:300], "url": c.video_url or c.url}
        for i, c in enumerate(candidates)
    ]
    user = (
        (
            "Today's news headlines, for context only. You CANNOT pick these — "
            "there is no video behind them. Use them only to judge what feels "
            "timely or over-covered:\n"
            + "\n".join(f"- {h}" for h in (context or [])[:40]) + "\n\n"
            if context else ""
        )
        + f"Pick up to {n} of these candidates, best first. Only the first few "
        f"will actually be posted — the rest are spares in case a download "
        f"fails, so order matters. Return fewer if the rest aren't good "
        f"enough.\n\n{json.dumps(listing, indent=2)}"
    )
    schema = _picks_schema(moods)

    raw: list[dict[str, Any]] = []
    tried: list[str] = []
    for name in providers:
        key = provider_key(name)
        if not key:
            LOG.debug("editorial: %s has no key — skipping", name)
            continue
        call = PROVIDER_CALLS.get(name)
        if not call:
            LOG.warning("editorial: unknown provider %r in config", name)
            continue
        model = models.get(name, "")
        LOG.info("asking %s (%s) to pick %d…", name, model, n)
        try:
            raw = call(system, user, schema, model, key)
        except Exception as exc:  # noqa: BLE001
            # Out of credit, rate-limited, model renamed, network — all the
            # same from here: note it and try the next provider.
            LOG.warning("editorial: %s failed (%s) — trying the next provider",
                        name, re.sub(r"\s+", " ", str(exc))[:300])
            tried.append(name)
            continue
        if raw:
            break
        LOG.warning("editorial: %s returned no usable picks — trying the next", name)
        tried.append(name)

    if not raw:
        LOG.error("no editorial provider returned picks (tried: %s)",
                  ", ".join(tried) or "none had keys")
        return []

    picks: list[Pick] = []
    for p in raw[:n]:
        idx = p.get("id")
        if isinstance(idx, str) and idx.isdigit():
            idx = int(idx)
        if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
            LOG.warning("editorial returned an out-of-range id: %r", idx)
            continue
        headline = (p.get("headline") or "").strip()[:max_headline]
        if not headline:
            continue
        mood = (p.get("mood") or "neutral").strip().lower()
        if mood not in moods:
            LOG.warning("editorial returned an unknown mood %r — using neutral", mood)
            mood = "neutral"
        picks.append(Pick(
            candidate=candidates[idx],
            headline=headline,
            caption=(p.get("caption") or "").strip(),
            reason=(p.get("reason") or "").strip(),
            mood=mood,
        ))
    return picks


def extract_representative_frame(media_path: Path, workdir: Path | None = None) -> Path | None:
    """
    Extract a representative still image frame from a video or return the image path.
    Used for AI vision analysis of layout, caption placement, and subject framing.
    """
    media_path = Path(media_path)
    if not media_path.exists():
        return None

    ext = media_path.suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return media_path

    out_dir = workdir or media_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_path = out_dir / f"{media_path.stem}_frame.jpg"

    dur = 0.0
    try:
        dur = render.probe_duration(media_path)
    except Exception:
        pass

    ss = "1.0"
    if dur > 0.5:
        target_sec = min(1.5, dur * 0.2)
        ss = f"{target_sec:.2f}"

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", ss,
        "-i", str(media_path),
        "-vframes", "1",
        "-q:v", "2",
        str(frame_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        if frame_path.exists() and frame_path.stat().st_size > 0:
            return frame_path
    except Exception as exc:
        LOG.debug("ffmpeg frame extract at %ss failed: %s; trying 0s", ss, exc)
        try:
            cmd[cmd.index("-ss") + 1] = "0.0"
            subprocess.run(cmd, capture_output=True, check=True)
            if frame_path.exists() and frame_path.stat().st_size > 0:
                return frame_path
        except Exception:
            pass

    return None


def analyze_visual_crop(
    media_path: Path,
    config: dict[str, Any] | None = None,
    workdir: Path | None = None,
) -> dict[str, Any]:
    """
    Analyze visual media to determine where essential content / joke setup lives,
    recommending optimal fit_mode ('contain' vs 'cover'), anchor coordinates, and aspect ratio.
    """
    import base64
    from PIL import Image

    frame_path = extract_representative_frame(media_path, workdir=workdir)
    if not frame_path or not frame_path.exists():
        return {
            "fit_mode": "cover",
            "anchor_x": 0.5,
            "anchor_y": 0.5,
            "aspect_ratio": "16:9",
            "has_top_text": False,
            "has_bottom_text": False,
            "mood": "",
            "reasoning": "Could not extract representative frame for analysis.",
        }

    try:
        with Image.open(frame_path) as im:
            w, h = im.size
    except Exception:
        w, h = 1920, 1080

    aspect = (w / h) if h > 0 else 1.777

    # Default fallback based on geometry
    if aspect < 0.85:
        fallback = {
            "fit_mode": "contain",
            "anchor_x": 0.5,
            "anchor_y": 0.2,
            "aspect_ratio": "1:1",
            "has_top_text": True,
            "has_bottom_text": False,
            "mood": "",
            "reasoning": "Vertical media detected: using contain to avoid cropping out top or bottom context.",
        }
    elif aspect < 1.15:
        fallback = {
            "fit_mode": "cover",
            "anchor_x": 0.5,
            "anchor_y": 0.5,
            "aspect_ratio": "1:1",
            "has_top_text": False,
            "has_bottom_text": False,
            "mood": "",
            "reasoning": "Square media detected.",
        }
    else:
        fallback = {
            "fit_mode": "cover",
            "anchor_x": 0.5,
            "anchor_y": 0.5,
            "aspect_ratio": "16:9",
            "has_top_text": False,
            "has_bottom_text": False,
            "mood": "",
            "reasoning": "Landscape media detected.",
        }

    cfg = config or {}
    ed = cfg.get("editorial", {})
    # An explicitly empty list means "no providers", not "use the defaults".
    providers = ed.get("providers")
    providers = ["gemini", "anthropic", "openai"] if providers is None else providers
    moods = available_moods(cfg)

    prompt = (
        "You are an expert video director and layout optimizer for short-form video cards (1080x1920 vertical canvas).\n"
        "Analyze this visual frame from a social media post (meme, joke, or clip).\n"
        "Identify where the essential content of the joke / key action / text is located so that cropping does NOT cut off the punchline or context.\n\n"
        "Specifically determine:\n"
        "1. Is there setup/caption text at the top?\n"
        "2. Is there punchline/subtitle text at the bottom?\n"
        "3. Where is the main visual subject (character, action)?\n"
        "4. What is the bounding box of all essential content? [ymin, xmin, ymax, xmax] (normalized 0.0 to 1.0)\n"
        "5. Recommend the fit mode:\n"
        "   - 'contain': When essential text or action spans top-to-bottom (e.g. a vertical meme with top text and center graphic), so that letterboxing/pillarboxing preserves 100% of the joke without cutoff.\n"
        "   - 'cover': When the frame can be safely cropped without losing any punchline or critical text.\n"
        "6. Recommend crop anchors:\n"
        "   - 'recommended_crop_anchor_y': float between 0.0 (top) and 1.0 (bottom). If there is top text, bias towards 0.0-0.2 so the text stays visible.\n"
        "   - 'recommended_crop_anchor_x': float between 0.0 (left) and 1.0 (right).\n"
        "7. Recommend aspect ratio for the card window:\n"
        "   - '16:9', '1:1', '4:5', or '9:16'.\n"
        "8. Judge the emotional register — how this should FEEL to a viewer. It "
        "selects the background music, so choose by feel, not by subject matter.\n"
        f"   - Choose exactly one of: {', '.join(moods)}.\n"
        "   - Only choose 'neutral' if the media genuinely has no emotional "
        "colour. Prefer a specific mood whenever one fits.\n\n"
        "Respond ONLY with a JSON object:\n"
        "{\n"
        '  "has_top_text": boolean,\n'
        '  "has_bottom_text": boolean,\n'
        '  "visual_subject_location": "top" | "center" | "bottom" | "full",\n'
        '  "content_bounding_box": [ymin, xmin, ymax, xmax],\n'
        '  "recommended_crop_anchor_y": float,\n'
        '  "recommended_crop_anchor_x": float,\n'
        '  "recommended_fit_mode": "contain" | "cover",\n'
        '  "recommended_aspect_ratio": "16:9" | "1:1" | "4:5" | "9:16",\n'
        f'  "mood": one of {json.dumps(moods)},\n'
        '  "reasoning": "brief explanation"\n'
        "}"
    )

    def shape(data: dict[str, Any]) -> dict[str, Any]:
        fit = str(data.get("recommended_fit_mode", "cover")).lower()
        ay = float(data.get("recommended_crop_anchor_y", 0.5))
        ax = float(data.get("recommended_crop_anchor_x", 0.5))
        asp = data.get("recommended_aspect_ratio", "16:9")
        if data.get("has_top_text") and fit == "cover" and ay > 0.3:
            ay = 0.1
        mood = str(data.get("mood", "")).strip().lower()
        return {
            "fit_mode": fit if fit in ("contain", "cover") else "contain",
            "anchor_x": max(0.0, min(1.0, ax)),
            "anchor_y": max(0.0, min(1.0, ay)),
            "aspect_ratio": asp if asp in ("16:9", "1:1", "4:5", "9:16") else "1:1",
            "has_top_text": bool(data.get("has_top_text")),
            "has_bottom_text": bool(data.get("has_bottom_text")),
            "mood": mood if mood in moods else "",
            "reasoning": str(data.get("reasoning", "")),
        }

    try:
        raw_bytes = frame_path.read_bytes()
        b64_img = base64.b64encode(raw_bytes).decode("utf-8")
    except Exception as exc:
        LOG.warning("Could not read frame for vision analysis: %s", exc)
        return fallback

    for p_name in providers:
        key = provider_key(p_name)
        if not key:
            continue
        try:
            if p_name == "gemini":
                models = ed.get("models", {}).get("gemini")
                if not models:
                    models = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-latest"]
                elif isinstance(models, str):
                    models = [m.strip() for m in models.split(",") if m.strip()]

                for mod in models:
                    try:
                        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mod}:generateContent?key={key}"
                        payload = {
                            "contents": [{
                                "parts": [
                                    {"text": prompt},
                                    {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}}
                                ]
                            }],
                            "generationConfig": {"responseMimeType": "application/json"}
                        }
                        import urllib.request
                        ssl_ctx = _ssl_context()
                        req = urllib.request.Request(
                            url,
                            data=json.dumps(payload).encode("utf-8"),
                            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
                        )
                        with urllib.request.urlopen(req, timeout=25, context=ssl_ctx) as resp:
                            res = json.loads(resp.read().decode("utf-8"))
                            text = res["candidates"][0]["content"]["parts"][0]["text"]
                            return shape(json.loads(text))
                    except Exception as mod_exc:
                        LOG.debug("gemini %s vision attempt error: %s", mod, mod_exc)
                        continue

            elif p_name == "anthropic":
                try:
                    import anthropic
                    client = anthropic.Anthropic(api_key=key)
                    model_name = ed.get("models", {}).get("anthropic", "claude-sonnet-4-5")
                    msg = client.messages.create(
                        model=model_name,
                        max_tokens=1000,
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_img}},
                                {"type": "text", "text": prompt}
                            ]
                        }]
                    )
                    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                    text = re.sub(r"^```[a-z]*\n|\n```$", "", text.strip())
                    return shape(json.loads(text))
                except Exception as ant_exc:
                    LOG.debug("anthropic vision error: %s", ant_exc)

            elif p_name == "openai":
                try:
                    import openai
                    client = openai.OpenAI(api_key=key)
                    model_name = ed.get("models", {}).get("openai", "gpt-5.6-luna")
                    resp = client.chat.completions.create(
                        model=model_name,
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                            ]
                        }],
                        response_format={"type": "json_object"}
                    )
                    text = resp.choices[0].message.content or "{}"
                    return shape(json.loads(text))
                except Exception as oai_exc:
                    LOG.debug("openai vision error: %s", oai_exc)

        except Exception as exc:
            LOG.warning("Vision analysis with provider %s failed: %s", p_name, exc)

    return fallback


def available_moods(config: dict[str, Any]) -> list[str]:
    """
    The moods that actually have tracks on disk.

    Offering a model the full configured list lets it confidently pick an empty
    folder, which pick_music then silently collapses to neutral — the choice is
    made and thrown away. Constraining the menu to stocked folders keeps the
    decision meaningful.
    """
    audio = config.get("audio", {})
    configured = audio.get("moods") or ["neutral"]
    base = Path(audio.get("library_dir", "assets/music"))
    if not base.is_absolute():
        base = ROOT / base
    exts = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus")
    stocked = [
        m for m in configured
        if (base / m).is_dir()
        and any(t.suffix.lower() in exts and not t.name.startswith(".")
                for t in (base / m).iterdir())
    ]
    return stocked or ["neutral"]


def pick_music(mood: str, config: dict[str, Any]) -> Path | None:
    """
    Choose a track for this mood from the local library.

    Layout: assets/music/<mood>/*.mp3 — a folder per mood, any number of tracks
    in each. Falls back to 'neutral', then to nothing at all: a missing track
    should mean a silent video, never a failed run.
    """
    import random

    audio = config.get("audio", {})
    if not audio.get("enabled", True):
        return None

    base = Path(audio.get("library_dir", "assets/music"))
    if not base.is_absolute():
        base = ROOT / base

    exts = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus")
    for candidate_mood in (mood, "neutral"):
        folder = base / candidate_mood
        if not folder.is_dir():
            continue
        tracks = sorted(t for t in folder.iterdir()
                        if t.suffix.lower() in exts and not t.name.startswith("."))
        if tracks:
            chosen = random.choice(tracks)
            if candidate_mood != mood:
                LOG.info("   no '%s' tracks — falling back to neutral", mood)
            LOG.info("   music: %s (%s)", chosen.name, candidate_mood)
            return chosen

    LOG.warning("   no music found under %s — rendering without a music bed", base)
    return None


# ==========================================================================
# Download
# ==========================================================================

def ytdlp_binary() -> str:
    """
    The project's own yt-dlp, not whatever PATH happens to find first.

    The studio server runs under launchd with Homebrew ahead of the venv on
    PATH, so a stale brew copy silently served every download — months behind
    on the extractor fixes these sites need.
    """
    pinned = ROOT / ".venv" / "bin" / "yt-dlp"
    if pinned.is_file() and os.access(pinned, os.X_OK):
        return str(pinned)
    return shutil.which("yt-dlp") or ""


def download_clip(url: str, dest_dir: Path, max_seconds: int,
                  cookies_file: str = "") -> tuple[Path | None, str]:
    """
    Fetch the complete source so editing can finish a sentence past the target.

    Returns (path, "") on success or (None, reason) on failure — the caller
    needs the reason to decide whether to move on to the next candidate.
    """
    ytdlp = ytdlp_binary()
    if not ytdlp:
        return None, "yt-dlp not found — pip install yt-dlp into .venv"

    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha1(url.encode()).hexdigest()[:12] + '-full'
    outtmpl = str(dest_dir / f"{stem}.%(ext)s")

    cmd = [
        ytdlp,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        # Some extractors (TikTok especially) fail intermittently on a URL
        # that works on the next attempt.
        "--extractor-retries", "3",
        # Cap the source height hard. The old selector ended in a bare "/b",
        # which on a 4K upload (NASA publishes in 4K) downloaded 2160p and made
        # ffmpeg decode-and-downscale it into a 936px-wide window — minutes of
        # CPU for pixels that get thrown away. 1080 is already >2x the window.
        "-f", ("bv*[height<=1080]+ba/b[height<=1080]/"
               "bv*[height<=1440]+ba/b[height<=1440]/b"),
        "--merge-output-format", "mp4",
        "-o", outtmpl,
    ]

    # On a datacenter IP, YouTube frequently demands proof you are not a bot.
    # A cookies file exported from a signed-in browser is the usual remedy.
    if cookies_file:
        if Path(cookies_file).exists():
            cmd += ["--cookies", cookies_file]
        else:
            LOG.warning("cookies file %s not found — continuing without it", cookies_file)

    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr.strip()[:400] or "yt-dlp returned a non-zero exit code"
        if "confirm you" in err.lower() or "not a bot" in err.lower():
            err += ("  [This is YouTube blocking a datacenter IP. Export cookies "
                    "from a signed-in browser and set posting.cookies_file.]")
        return None, err

    matches = sorted(dest_dir.glob(f"{stem}.*"))
    videos = [m for m in matches if m.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if not videos:
        return None, "yt-dlp produced no video file"

    clip = videos[0]
    # Log what we actually got — source resolution is the single biggest lever
    # on render time, so make it visible rather than a mystery.
    try:
        dims = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(clip)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        LOG.info("   downloaded %s (%s, %.1f MB)", clip.name, dims,
                 clip.stat().st_size / 1e6)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return clip, ""


# ==========================================================================
# Upload
# ==========================================================================

def download_images(urls: list[str], dest_dir: Path, stem: str) -> list[Path]:
    """
    Fetch story images to disk. Returns only the ones that actually arrived.

    A partial set is still usable — one image is a valid card — so a single
    failed download does not sink the post.
    """
    import urllib.request

    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _ssl_context()

    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, url in enumerate(urls):
        ext = Path(url.split("?")[0]).suffix.lower()
        if ext not in IMAGE_EXTS:
            ext = ".jpg"
        target = dest_dir / f"{stem}_img{i}{ext}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
                target.write_bytes(resp.read())
            saved.append(target)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("   image %d failed (%s): %s", i + 1, url[:60], exc)
    if saved:
        LOG.info("   downloaded %d/%d image(s)", len(saved), len(urls))
    return saved


def find_recent_upload(title: str, state_path: Path | None = None,
                       within_minutes: int = 90) -> str | None:
    """
    Did a video with this title just land on the channel?

    A dropped connection can hide a *successful* upload — YouTube accepted the
    file and published it, but the client never read the reply. Retrying then
    posts a duplicate. This checks the public uploads feed with the API key, so
    it needs no extra OAuth scope.
    """
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key or not title.strip():
        return None
    state_path = state_path or (ROOT / "state.json")
    try:
        posted = json.loads(state_path.read_text()).get("posted", {})
    except (OSError, ValueError):
        posted = {}
    seed = next((v.get("youtube_id") for v in reversed(list(posted.values()))
                 if v.get("youtube_id")), None)
    if not seed:
        return None

    import urllib.parse
    import urllib.request

    def api(path: str, **params) -> dict:
        params["key"] = key
        url = f"https://www.googleapis.com/youtube/v3/{path}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=25, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        vids = api("videos", part="snippet", id=seed)["items"]
        channel = vids[0]["snippet"]["channelId"]
        details = api("channels", part="contentDetails", id=channel)["items"][0]
        uploads = details["contentDetails"]["relatedPlaylists"]["uploads"]
        items = api("playlistItems", part="snippet", maxResults=10,
                    playlistId=uploads)["items"]
    except Exception as exc:  # noqa: BLE001 — a failed check must not block
        LOG.debug("could not check for a recent upload: %s", exc)
        return None

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=within_minutes)
    wanted = title.strip().casefold()
    for item in items:
        snippet = item["snippet"]
        try:
            when = dt.datetime.fromisoformat(
                snippet["publishedAt"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if when >= cutoff and snippet.get("title", "").strip().casefold() == wanted:
            return snippet["resourceId"]["videoId"]
    return None


def upload_youtube(
    video: Path,
    title: str,
    description: str,
    config: dict[str, Any],
    token_path: Path,
    privacy: str = "",
    publish_at: str = "",
    report=None,
) -> str | None:
    """
    Upload to YouTube Shorts via Data API v3. Returns the video id.

    privacy overrides posting.privacy for this one video. publish_at is an
    RFC3339 timestamp: YouTube requires the video be private until then, so a
    scheduled upload is always uploaded private and released by YouTube.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from googleapiclient.errors import HttpError
    except ImportError:
        LOG.error("Google client libs missing: pip install google-api-python-client "
                  "google-auth-httplib2 google-auth-oauthlib")
        return None

    if not token_path.exists():
        LOG.error("%s not found — run: python3 youtube_auth.py", token_path)
        return None

    creds = Credentials.from_authorized_user_file(
        str(token_path), ["https://www.googleapis.com/auth/youtube.upload"]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())

    posting = config.get("posting", {})
    # #Shorts in the title/description is what flags a vertical <3min upload
    # as a Short for the algorithm.
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": posting.get("tags", ["shorts", "news"]),
            "categoryId": str(posting.get("category_id", "25")),
        },
        "status": {
            "privacyStatus": privacy or posting.get("privacy", "public"),
            "selfDeclaredMadeForKids": False,
        },
    }

    if publish_at:
        # YouTube only honours publishAt on a private video, and rejects the
        # request outright if it is anything else.
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = publish_at

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    # Real chunks, not chunksize=-1: a single-request upload restarts from zero
    # when the connection drops, and the drop usually happens near the end.
    # Chunked resumable upload picks up from the last byte YouTube confirmed.
    media = MediaFileUpload(str(video), chunksize=4 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    import http.client
    import ssl as _ssl
    transient = (TimeoutError, ConnectionError, OSError, _ssl.SSLError,
                 http.client.HTTPException)
    response, drops = None, 0
    while response is None:
        try:
            status, response = request.next_chunk(num_retries=3)
            if status and report:
                report(f"Uploading {int(status.progress() * 100)}%")
        except HttpError as exc:
            if exc.resp.status in (500, 502, 503, 504):
                drops += 1
                if drops > MAX_UPLOAD_DROPS:
                    LOG.error("YouTube upload failed after %d retries: %s", drops, exc)
                    return None
                if report:
                    report(f"YouTube is busy — retrying ({drops}/{MAX_UPLOAD_DROPS})")
                time.sleep(min(2 ** drops, 30))
                continue
            LOG.error("YouTube upload failed: %s", exc)
            return None
        except transient as exc:
            drops += 1
            if drops > MAX_UPLOAD_DROPS:
                LOG.error("Connection lost %d times during upload: %s", drops, exc)
                return None
            # The bytes already accepted stay accepted; next_chunk resumes.
            if report:
                report(f"Connection dropped — resuming ({drops}/{MAX_UPLOAD_DROPS})")
            LOG.warning("connection dropped during upload (%d/%d): %s",
                        drops, MAX_UPLOAD_DROPS, exc)
            time.sleep(min(2 ** drops, 30))

    return response.get("id")


# ==========================================================================
# Pipeline
# ==========================================================================

def notify(config: dict[str, Any], text: str) -> None:
    """
    Send a Telegram message. Never raises.

    Running unattended on a server, a failure nobody hears about is the same
    as no pipeline at all — so this is the only thing standing between a
    broken run and silence. It swallows its own errors on purpose: an alert
    that crashes the run it is reporting on would be worse than useless.
    """
    cfg = config.get("notify", {})
    if not cfg.get("enabled", True):
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = str(cfg.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", ""))
    if not token or not chat:
        LOG.debug("telegram not configured — skipping notification")
        return

    import urllib.parse
    import urllib.request
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat,
            "text": text[:4000],
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        LOG.debug("telegram notification sent")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("telegram notification failed: %s", exc)


SUMMARY_PREFIX = "MKEN_SUMMARY "


def emit_summary(summary: dict[str, Any]) -> None:
    """
    Print a single machine-readable line to stdout.

    Logging goes to stderr, so stdout stays clean for exactly this. runner.py
    (and therefore n8n) parses it to tell "ran fine but posted nothing" apart
    from "ran fine and posted two".
    """
    print(SUMMARY_PREFIX + json.dumps(summary), flush=True)


def _finish_post(pick: "Pick", c: "Candidate", video: Path,
                 config: dict[str, Any], state: "State",
                 args: argparse.Namespace) -> bool:
    """
    Title, describe and upload a finished render. Shared by both formats.

    Both the clip card and the story card end the same way, and having one
    copy means a change to crediting or the dry-run output can't drift
    between them.
    """
    # c.source is an internal discovery label ("youtube:NASA", "rss:bbc") —
    # the prefix is for logs, not for a public description.
    who = re.sub(r"^(youtube|rss|reddit|wikipedia|wikimedia):", "", c.source).strip()
    credit = f"Source: {c.url}"
    if who:
        credit += f"\nvia {who}"
    if c.attribution:
        # CC-BY material must be credited, and the description is where a
        # viewer (or a rights holder) will look for it. Label it for what it
        # actually is: a story card credits a still, a clip credits footage.
        label = "Image" if c.kind == "story" else "Footage"
        credit += f"\n{label}: {c.attribution}"
    if c.licence == "public-domain":
        credit += "\nPublic domain."
    parts = [p for p in (pick.caption.strip(), credit, "#Shorts") if p]
    description = "\n\n".join(parts)
    title = f"{pick.headline[:90]} #Shorts"

    record = studio.get('videos', video.stem)
    if record:
        record.update(title=title, description=description)
        studio.put('videos', record['id'], record)
    if args.dry_run:
        LOG.info("   DRY RUN — not uploading")
        LOG.info("   title: %s", title)
        LOG.info("   description: %s", description.replace("\n", " | "))
        return True

    studio.progress('Uploading', video_id=video.stem)
    if record:
        record['status'] = 'uploading'
        studio.put('videos', record['id'], record)
    try:
        vid = upload_youtube(video, title, description, config, Path(args.token))
    except Exception:
        if record:
            record.update(status='upload_unknown', error='Upload interrupted. Check YouTube Studio before retrying.')
            studio.put('videos', record['id'], record)
        raise
    if record:
        record.update(status='uploaded' if vid else 'upload_unknown', youtube_id=vid or '',
                      error='' if vid else 'Upload did not return a video ID. Check YouTube Studio before retrying.')
        studio.put('videos', record['id'], record)
    if vid:
        LOG.info("   uploaded: https://youtube.com/watch?v=%s", vid)
        state.mark_posted(pick, video_id=vid, dry_run=False)
        return True
    LOG.error("   upload failed")
    return False


def run(args: argparse.Namespace) -> int:
    config = json.loads(Path(args.config).read_text())

    # Fail before discovery, not after. Discovery takes ~20s and hits every
    # configured feed; there is no point spending that to die on a key we
    # could have rejected instantly.
    if not args.no_llm:
        ok, why = check_api_key(config=config)
        if not ok:
            sys.exit(why)

    state = State(Path(args.state))
    posting = config.get("posting", {})

    n = args.limit or int(posting.get("clips_per_run", 1))
    max_seconds = int(posting.get("max_clip_seconds", 30))

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    studio.progress("Finding stories")
    LOG.info("discovering…")
    candidates, context = discover(config, state=state)
    LOG.info("%d postable candidates, %d context headlines",
             len(candidates), len(context))

    max_attempts = int(config.get("posting", {}).get("max_download_attempts", 3))
    fresh = [c for c in candidates if not state.is_seen(c, max_attempts)]
    LOG.info("%d after dedupe against state.json", len(fresh))
    if not fresh:
        LOG.info("nothing new — exiting")
        summary = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "candidates": len(candidates), "fresh": 0, "picks": 0, "posted": 0,
                   "dry_run": args.dry_run, "note": "no fresh candidates"}
        state.log_run(summary)
        state.save()
        emit_summary(summary)
        return 0

    # Prefer candidates that already have a downloadable clip.
    fresh.sort(key=lambda c: (bool(c.video_url), c.score), reverse=True)
    fresh = fresh[: int(config.get("editorial", {}).get("max_candidates", 40))]

    # Ask for more than we need. Downloads fail for ordinary reasons — an
    # upcoming livestream, a region block, a deleted video — and without
    # spares a single failure means the run posts nothing.
    backups = int(config.get("editorial", {}).get("backup_picks", 2))
    want = n + backups

    studio.progress("Choosing stories")
    if args.no_llm:
        LOG.warning("--no-llm: taking the top %d by score, headline = title", want)
        picks = [Pick(candidate=c, headline=c.title, caption="") for c in fresh[:want]]
    else:
        LOG.info("editorial: need %d (%d to post + %d spare) from %d candidates…",
                 want, n, backups, len(fresh))
        picks = pick_stories(fresh, config, want, context)
    LOG.info("%d picks (need %d)", len(picks), n)

    posted = 0
    failures = 0
    completed_headlines = []
    for pick in picks:
        # picks may include backups; stop once we've filled the run's quota.
        if posted >= n:
            break

        c = pick.candidate
        studio.progress("Preparing media", headline=pick.headline)
        source_url = c.video_url or c.url
        LOG.info("→ [%s] %s", c.kind, pick.headline)
        LOG.info("   source: %s", source_url)

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = OUT_DIR / f"{stamp}-{c.key}.mp4"
        poster = OUT_DIR / f"{stamp}-{c.key}.png"

        # ---- story format: images + mascot, no clip to download ------------
        if c.kind == "story":
            import render_story
            story_cfg = config.get("story", {})
            if not story_cfg.get("enabled", True):
                LOG.warning("   story format disabled in config — skipping")
                continue
            imgs = download_images(c.images, WORK_DIR, c.key)
            if not imgs:
                LOG.warning("   skipped — no images could be downloaded")
                state.mark_failed(c, "all image downloads failed")
                failures += 1
                state.save()
                continue
            pick.mood = analyze_visual_crop(
                imgs[0], config=config, workdir=WORK_DIR
            ).get("mood") or pick.mood
            music = pick_music(pick.mood, config)
            studio.progress("Rendering", headline=pick.headline)
            try:
                result = render_story.render_story(
                    headline=pick.headline,
                    commentary=pick.caption or c.commentary,
                    images=[str(p) for p in imgs],
                    mascot=story_cfg.get("mascot", "assets/mascot.mp4"),
                    out=out_path,
                    config=config,
                    duration=float(story_cfg.get("duration_seconds", 12)),
                    music=music,
                    poster=poster,
                    workdir=WORK_DIR,
                )
            except Exception as exc:  # noqa: BLE001
                LOG.error("   story render failed: %s", exc)
                state.mark_failed(c, f"story render: {exc}")
                failures += 1
                state.save()
                continue
            LOG.info("   rendered %s (%.1fs)", result.output.name, result.duration)
            studio.draft(pick, result.output, poster, config, '', '',
                         images=[str(p) for p in imgs], template=result.template,
                         music=str(music) if music else '')
            state.mark_seen(c)
            if _finish_post(pick, c, result.output, config, state, args):
                posted += 1
                completed_headlines.append(pick.headline)
            else:
                failures += 1
            state.save()
            continue

        clip, err = download_clip(
            source_url, WORK_DIR, max_seconds,
            cookies_file=os.environ.get("YTDLP_COOKIES")
            or posting.get("cookies_file", ""),
        )
        if not clip:
            # Common and expected: a channel feed lists scheduled livestreams
            # that have no video yet. Move to the next candidate rather than
            # ending the run empty-handed.
            LOG.warning("   skipped — %s", err)
            state.mark_failed(c, err)
            failures += 1
            state.save()
            continue

        studio.progress("Rendering", headline=pick.headline)
        try:
            result = render.render_card(
                video=clip,
                headline=pick.headline,
                out=out_path,
                config=config,
                max_seconds=max_seconds,
                poster=poster,
                workdir=WORK_DIR,
                mood=pick.mood,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.error("   render failed: %s", exc)
            state.mark_failed(c, f"render: {exc}")
            failures += 1
            state.save()
            continue

        pick.mood = result.mood
        studio.draft(pick, result.output, poster, config, '', '',
                     source_clip=str(clip), music=str(result.music) if result.music else '')
        # Reserved by a durable draft; retries upload this draft, not a new discovery.
        state.mark_seen(c)
        LOG.info("   rendered %s (%.1fs)", result.output.name, result.duration)

        if _finish_post(pick, c, result.output, config, state, args):
            posted += 1
            completed_headlines.append(pick.headline)
        else:
            failures += 1
        state.save()

    summary = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "candidates": len(candidates),
        "fresh": len(fresh),
        "picks": len(picks),
        "posted": 0 if args.dry_run else posted,
        "rendered": posted,
        "failed": failures,
        "dry_run": args.dry_run,
        "headlines": completed_headlines,
    }
    state.log_run(summary)
    state.save()
    LOG.info("done — %d %s", posted, "rendered (dry run)" if args.dry_run else "posted")
    emit_summary(summary)

    notify_on = config.get("notify", {}).get("on", ["failure", "empty"])
    label = "rendered (dry run)" if args.dry_run else "posted"
    if posted == 0 and picks:
        if "empty" in notify_on:
            notify(config, f"⚠️ *Miscellaneous Ken* — ran but posted nothing.\n"
                           f"{len(picks)} pick(s), all failed to download or render.")
    elif posted and "success" in notify_on:
        heads = "\n".join(f"• {h}" for h in summary["headlines"][:posted])
        notify(config, f"✅ *Miscellaneous Ken* — {posted} {label}.\n{heads}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Miscellaneous Ken posting agent")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--token", default=str(ROOT / "token.json"))
    ap.add_argument("--dry-run", action="store_true",
                    help="render everything but skip the upload")
    ap.add_argument("--limit", type=int, default=0,
                    help="override clips_per_run")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the Claude call (top-scoring candidates, raw titles)")
    ap.add_argument("--check-sources", action="store_true",
                    help="resolve the configured channels and show what each returns")
    ap.add_argument("--notify-test", action="store_true",
                    help="send a test Telegram message and exit")
    ap.add_argument("--check-music", action="store_true",
                    help="show how many tracks each mood folder has")
    ap.add_argument("--upload", metavar="FILE",
                    help="upload an already-rendered mp4 and exit")
    ap.add_argument("--title", default="", help="title for --upload")
    ap.add_argument("--description", default="", help="description for --upload")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    studio.load_secrets()

    # -v means "tell me more about the pipeline", not "dump every HTTP frame".
    # Whitelist, don't blacklist: naming the noisy libraries doesn't work,
    # because the anthropic SDK vendors its HTTP stack under names like
    # "httpcore2" that no blacklist would predict. Instead the root logger
    # stays at WARNING — so third-party DEBUG/INFO never prints, whatever it
    # calls itself — and only our own loggers are turned up.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in ("mken", "mken.render"):
        logging.getLogger(name).setLevel(
            logging.DEBUG if args.verbose else logging.INFO
        )
    try:
        if args.check_sources:
            return check_sources(json.loads(Path(args.config).read_text()))
        if args.check_music:
            return check_music(json.loads(Path(args.config).read_text()))
        if args.upload:
            cfg = json.loads(Path(args.config).read_text())
            path = Path(args.upload)
            if not path.exists():
                sys.exit(f"not found: {path}")
            if not args.title:
                sys.exit("--upload needs --title")
            LOG.info("uploading %s (%.1f MB) as %s",
                     path.name, path.stat().st_size / 1e6,
                     cfg.get("posting", {}).get("privacy", "private"))
            with studio.pipeline_lock():
                vid = upload_youtube(path, args.title, args.description, cfg,
                                     Path(args.token))
            if not vid:
                return 1
            LOG.info("uploaded: https://youtube.com/watch?v=%s", vid)
            print(f"https://youtube.com/watch?v={vid}")
            return 0
        if args.notify_test:
            cfg = json.loads(Path(args.config).read_text())
            notify(cfg, "🔔 *Miscellaneous Ken* — notification test. "
                        "If you can read this, alerting works.")
            print("Sent (check Telegram). If nothing arrives, verify "
                  "TELEGRAM_BOT_TOKEN and the chat id.")
            return 0
        with studio.pipeline_lock():
            return run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        # An unhandled crash on a server is the case most likely to go
        # unnoticed, so it is the case that most needs to shout.
        LOG.exception("run failed")
        try:
            notify(json.loads(Path(args.config).read_text()),
                   f"🔴 *Miscellaneous Ken* — run crashed.\n`{type(exc).__name__}: {exc}`")
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
