#!/usr/bin/env python3
"""Loopback-only companion for the Mac app. No third-party web dependencies."""
from __future__ import annotations
import copy
import json
import mimetypes
import os
import re
import secrets
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote
import studio_store as store

ROOT = store.ROOT
PORT = int(os.environ.get('MKEN_STUDIO_PORT', '8766'))
CSRF = secrets.token_urlsafe(32)
GUARD = threading.RLock()
ACTIVE = None
ONLINE = None
STOP = threading.Event()
KEYS = {'gemini':'GEMINI_API_KEY', 'anthropic':'ANTHROPIC_API_KEY', 'openai':'OPENAI_API_KEY', 'youtube':'YOUTUBE_API_KEY', 'reddit_client_id':'REDDIT_CLIENT_ID', 'reddit_client_secret':'REDDIT_CLIENT_SECRET'}
DEFAULT_AUTOMATION = dict(enabled=False, interval_hours=5, mode='preview', next_run=0)
MAX_UPLOAD_BYTES = 2 * 1024**3
BROWSER_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124 Safari/537.36')

def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else copy.deepcopy(default)

def write_json(path, data, private=False):
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if private:
        path.chmod(0o600)

def automation():
    return {**DEFAULT_AUTOMATION, **read_json(ROOT/'studio-settings.json', {})}

def environment():
    env = os.environ.copy()
    for key, value in read_json(ROOT/'studio-secrets.json', {}).items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    return env

def redact(text):
    for key, value in environment().items():
        if not any(part in key.upper() for part in ('KEY', 'TOKEN', 'SECRET', 'COOKIE')) or len(value) < 6:
            continue
        if value:
            text = text.replace(value, '[hidden]')
    # Provider, OAuth and notification diagnostics can contain credential URLs.
    text = re.sub(r'(https?://\S+)[?]\S+', r'\1?[hidden]', text)
    text = re.sub(r'(?i)(key|token|secret|authorization)([=: ]+)[^\s,]+', r'\1\2[hidden]', text)
    return text[-8000:]

def check_online():
    for host in ('www.google.com', 'en.wikipedia.org'):
        try:
            with socket.create_connection((host, 443), timeout=3):
                return True
        except OSError:
            pass
    return False

def run_job(job, command):
    global ACTIVE
    log = []
    proc = None
    timer = None
    timed_out = threading.Event()
    try:
        proc = subprocess.Popen(command, cwd=ROOT, env=environment(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        def expire():
            timed_out.set()
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            def force_stop():
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            killer = threading.Timer(5, force_stop)
            killer.daemon = True
            killer.start()
        timer = threading.Timer(1800, expire)
        timer.daemon = True
        timer.start()
        for line in proc.stdout:
            if line.startswith('MKEN_PROGRESS '):
                try:
                    job['stage'] = json.loads(line[14:])['stage']
                except (ValueError, KeyError):
                    pass
            elif line.startswith('MKEN_SUMMARY '):
                job['summary'] = json.loads(line[13:])
            else:
                log.append(redact(line.rstrip()))
            job['log'] = '\n'.join(log[-35:])[-8000:]
            store.put('jobs', job['id'], job)
        code = proc.wait()
        summary = job.get('summary', {})
        job.update(status='failed' if code or timed_out.is_set() else 'completed',
            stage='Timed out' if timed_out.is_set() else ('Failed' if code else 'Complete'), finished_at=store.now())
        if not code and summary.get('picks') and summary.get('failed') and not summary.get('rendered', summary.get('posted')):
            job.update(status='failed', stage='No videos completed')
    except Exception as exc:
        job.update(status='failed', stage='Failed', log=redact(str(exc)), finished_at=store.now())
    finally:
        if timer:
            timer.cancel()
        store.put('jobs', job['id'], job)
        with GUARD:
            ACTIVE = None

def start_job(action, key='', automatic=False):
    global ACTIVE
    with GUARD:
        if ACTIVE or store.busy():
            raise ValueError('A job is already running. Please wait for it to finish.')
        python = str(ROOT/'.venv/bin/python3')
        if action in ('preview', 'run', 'render') and shutil.disk_usage(ROOT).free < 512 * 1024 * 1024:
            raise ValueError('Free at least 512 MB of disk space before creating videos')
        if action in ('preview', 'run'):
            command = [python, '-u', str(ROOT/'agent.py'), '-v']
            if action == 'preview':
                command.append('--dry-run')
        elif action in ('upload', 'render'):
            if not store.get('videos', key):
                raise ValueError('Video not found')
            record = store.get('videos', key)
            if record['status'] in ('uploaded', 'upload_unknown'):
                raise ValueError('This video is already uploaded or needs its upload checked first')
            if action == 'upload' and record['status'] != 'ready':
                raise ValueError('Render pending edits before uploading')
            command = [python, '-u', str(ROOT/'studio_worker.py'), action, key]
        elif action == 'youtube_connect':
            command = [python, '-u', str(ROOT/'youtube_auth.py'), '--force', '--port', '0']
        elif action in ('sources', 'music'):
            command = [python, '-u', str(ROOT/'agent.py'), '--check-'+action]
        else:
            raise ValueError('Unknown action')
        if sys.platform == 'darwin' and action != 'youtube_connect':
            command = ['/usr/bin/caffeinate', '-i', *command]
        job = dict(id=uuid.uuid4().hex, action=action, status='running', stage='Starting',
                   created_at=store.now(), automatic=automatic, log='')
        ACTIVE = job['id']
        store.put('jobs', job['id'], job)
        threading.Thread(target=run_job, args=(job, command), daemon=True).start()
        return job

def scheduler():
    global ONLINE
    while not STOP.is_set():
        ONLINE = check_online()
        with GUARD:
            cfg = automation()
            if cfg['enabled'] and ONLINE and time.time() >= cfg['next_run'] and not ACTIVE and not store.busy() and shutil.disk_usage(ROOT).free >= 512 * 1024 * 1024:
                # Persist the next slot before launch; restarts cannot trigger a tight loop.
                cfg['next_run'] = time.time() + cfg['interval_hours'] * 3600
                write_json(ROOT/'studio-settings.json', cfg)
                try:
                    start_job('preview' if cfg['mode'] == 'preview' else 'run', automatic=True)
                except ValueError:
                    pass
        STOP.wait(30)

# Explicit edit surface: preserve unexposed configuration and reject arbitrary paths.
FIELDS = {
 'account': {'name':str, 'handle':str},
 'editorial': {'system_prompt':str, 'providers':list, 'models':dict},
 'posting': {'clips_per_run':int, 'max_clip_seconds':int, 'privacy':str},
 'story': {'enabled':bool, 'duration_seconds':int},
 'audio': {'enabled':bool, 'music_volume':float, 'source_audio_volume':float},
 'discovery': {'youtube_queries':list, 'wikipedia_queries':list, 'youtube_channels':list, 'news_rss':list, 'reddit_categories':dict},
 'layout': {'headline_size':int, 'border_color':str},
 'story_layout': {'template':str, 'theme':str, 'show_subscribe':bool},
}

def validate_config(changes):
    if not isinstance(changes, dict):
        raise ValueError('Settings must be an object')
    cfg = read_json(ROOT/'config.json')
    for section, values in changes.items():
        if section not in FIELDS or not isinstance(values, dict):
            raise ValueError('Unsupported settings section')
        for name, value in values.items():
            kind = FIELDS[section].get(name)
            if kind is None or not (type(value) is kind or kind is float and type(value) in (int, float)):
                raise ValueError('Invalid value for '+name)
            if isinstance(value, str) and len(value) > 12000:
                raise ValueError(name+' is too long')
            cfg.setdefault(section,{})[name] = value
    p = cfg['posting']
    if not 1 <= p['clips_per_run'] <= 6 or not 3 <= p['max_clip_seconds'] <= 180:
        raise ValueError('Choose 1–6 videos per run and 3–180 seconds per clip')
    if p['privacy'] not in ('private','unlisted','public'):
        raise ValueError('Invalid visibility')
    if not 3 <= cfg['story']['duration_seconds'] <= 60:
        raise ValueError('Story duration must be 3–60 seconds')
    if any(not 0 <= cfg['audio'][k] <= 2 for k in ('music_volume','source_audio_volume')):
        raise ValueError('Audio volume must be 0–2')
    sl = cfg.get('story_layout', {})
    if sl.get('template', 'auto') not in ('auto', 'reaction_card', 'classic', 'caption_card'):
        raise ValueError('Invalid story template')
    if sl.get('theme', 'auto') not in ('auto', 'light', 'dark'):
        raise ValueError('Invalid card theme')
    providers = cfg['editorial']['providers']
    if not providers or len(set(providers)) != len(providers) or any(x not in ('gemini','anthropic','openai') for x in providers):
        raise ValueError('Choose each supported editorial provider at most once')
    models = cfg['editorial']['models']
    for provider, model in models.items():
        if provider not in KEYS or not isinstance(model, (str, list)):
            raise ValueError('Invalid provider model')
        if provider != 'gemini' and not isinstance(model,str):
            raise ValueError(provider+' accepts one model name')
        vals = [model] if isinstance(model,str) else model
        if not vals or any(not isinstance(v,str) or not v.strip() or len(v)>150 for v in vals):
            raise ValueError('Enter at least one model name')
    for name in ('youtube_queries','wikipedia_queries','news_rss'):
        items = cfg['discovery'][name]
        if len(items)>40 or any(not isinstance(v,str) or len(v)>500 for v in items):
            raise ValueError('Use up to 40 short source entries')
    for url in cfg['discovery']['news_rss']:
        if urlsplit(url).scheme != 'https':
            raise ValueError('RSS sources must use HTTPS')
    channels = cfg['discovery']['youtube_channels']
    if len(channels)>40:
        raise ValueError('Use up to 40 channels')
    for c in channels:
        if isinstance(c,str):
            if not c.strip() or len(c)>300: raise ValueError('Invalid channel')
        elif isinstance(c,dict):
            if set(c)-{'name','channel','licence'} or any(not isinstance(v,str) or len(v)>300 for v in c.values()) or not isinstance(c.get('channel'),str) or not c['channel'].strip():
                raise ValueError('Invalid channel')
            if c.get('licence','') not in ('','cc','public-domain'):
                raise ValueError('Invalid source licence')
        else:
            raise ValueError('Invalid channel')
    if not 30 <= cfg['layout']['headline_size'] <= 110 or not re.fullmatch('#[0-9A-Fa-f]{6}',cfg['layout']['border_color']):
        raise ValueError('Invalid headline size or border color')
    return cfg

def inspect_reddit_url(url: str) -> dict:
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    parsed = urlsplit(url)
    if 'reddit.com' not in parsed.netloc and 'redd.it' not in parsed.netloc:
        raise ValueError('Enter a valid Reddit link (e.g. reddit.com/r/... or redd.it/...)')

    sub_match = re.search(r'/r/([^/?#]+)', parsed.path, re.IGNORECASE)
    sub = sub_match.group(1) if sub_match else ''

    cfg = read_json(ROOT / 'config.json', {})
    categories = cfg.get('discovery', {}).get('reddit_categories', {})
    category = 'Reddit'
    if sub:
        for cat, subs in categories.items():
            if any(s.lower() == sub.lower() for s in subs):
                category = cat
                break

    title = ''
    author = ''
    thumbnail_url = ''
    caption = ''
    media_url = ''
    image_urls: list[str] = []
    video_url = ''
    is_video = False
    is_gallery = False

    # 1. Direct media URL
    clean_path = parsed.path.lower()
    if any(h in parsed.netloc for h in ('packaged-media.redd.it', 'v.redd.it', 'i.redd.it')) or clean_path.endswith(('.mp4', '.mov', '.webm', '.jpg', '.jpeg', '.png', '.webp', '.gif')):
        if clean_path.endswith(('.mp4', '.mov', '.webm')) or 'v.redd.it' in parsed.netloc or 'packaged-media.redd.it' in parsed.netloc:
            is_video = True
            video_url = url
            media_url = url
            title = 'Video from Reddit'
        else:
            image_urls = [url]
            media_url = url
            title = 'Image from Reddit'

    # 2. Try fetching post JSON for reddit post links
    if not title or ('comments' in parsed.path or 'gallery' in parsed.path):
        import urllib.request, ssl, certifi
        import scrape_reddit
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        json_url = url.split('?')[0].rstrip('/') + '.json'

        token = scrape_reddit.get_oauth_token()
        headers = {'User-Agent': scrape_reddit.DEFAULT_USER_AGENT}
        if token:
            headers['Authorization'] = f"bearer {token}"
            json_url = json_url.replace("https://www.reddit.com", "https://oauth.reddit.com").replace("https://reddit.com", "https://oauth.reddit.com")

        try:
            req = urllib.request.Request(json_url, headers=headers)
            with urllib.request.urlopen(req, timeout=8, context=ssl_ctx) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                post_data = {}
                if isinstance(data, list) and data:
                    children = data[0].get('data', {}).get('children', [])
                    if children:
                        post_data = children[0].get('data', {})
                elif isinstance(data, dict):
                    children = data.get('data', {}).get('children', [])
                    if children:
                        post_data = children[0].get('data', {})

                if post_data:
                    title = post_data.get('title', '')
                    author = post_data.get('author', '')
                    sub = post_data.get('subreddit', sub)
                    caption = post_data.get('selftext', '')
                    thumbnail_url = post_data.get('thumbnail', '')
                    img_list, vid, p_med, vid_flag, gal_flag = scrape_reddit.extract_reddit_media(post_data)
                    image_urls = img_list
                    video_url = vid
                    media_url = p_med
                    is_video = vid_flag
                    is_gallery = gal_flag
        except Exception:
            pass

    # 3. Fallback to oEmbed
    if not title:
        try:
            import urllib.request, ssl, certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            oembed_url = f"https://www.reddit.com/oembed?url={quote(url)}"
            req = urllib.request.Request(oembed_url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'})
            with urllib.request.urlopen(req, timeout=8, context=ssl_ctx) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                title = data.get('title', '')
                author = data.get('author_name', '')
                if not thumbnail_url:
                    thumbnail_url = data.get('thumbnail_url', '')
                if not media_url and thumbnail_url:
                    media_url = thumbnail_url
        except Exception:
            pass

    if not title:
        parts = [p for p in parsed.path.strip('/').split('/') if p]
        if 'comments' in parts:
            idx = parts.index('comments')
            if len(parts) > idx + 2:
                slug = parts[idx + 2].replace('_', ' ')
                title = slug.capitalize()

    return {
        'url': url,
        'title': title,
        'subreddit': sub,
        'category': category,
        'author': author,
        'thumbnail_url': thumbnail_url or media_url,
        'media_url': media_url,
        'image_urls': image_urls,
        'video_url': video_url,
        'caption': caption,
        'is_video': is_video,
        'is_gallery': is_gallery,
    }

def manual_track(config: dict) -> tuple[str, float, str]:
    """The fixed track for hand-added posts: (path, start_seconds, credit)."""
    audio = config.get('audio') or {}
    raw = (audio.get('manual_track') or '').strip()
    if not raw or not audio.get('enabled', True):
        return '', 0.0, ''
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return '', 0.0, ''
    try:
        start = max(0.0, float(audio.get('manual_track_start_seconds', 0) or 0))
    except (TypeError, ValueError):
        start = 0.0
    return str(path), start, (audio.get('manual_track_credit') or '').strip()


def apply_template(config: dict, template: str) -> None:
    """Per-import override of story_layout.template; 'auto' keeps the global setting."""
    if template not in ('reaction_card', 'classic', 'caption_card'):
        return
    config.setdefault('story_layout', {})['template'] = template


def apply_framing(config: dict, framing: str) -> None:
    config.setdefault('layout', {})
    if framing == 'contain':
        config['layout']['video_fit'] = 'contain'
        config['layout']['video_aspect'] = 'auto'
    elif framing == 'top':
        config['layout']['video_fit'] = 'cover'
        config['layout']['background_anchor_y'] = 0.1
    elif framing == 'cover':
        config['layout']['video_fit'] = 'cover'
        config['layout']['background_anchor_y'] = 0.5
    else:  # auto / smart
        config['layout']['video_fit'] = 'auto'
        config['layout']['video_aspect'] = 'auto'


LINK_HOSTS_BLOCKED = ('localhost', '127.', '0.0.0.0', '::1', '169.254.', '10.',
                      '192.168.', '172.16.', '172.17.', '172.18.', '172.19.',
                      '172.2', '172.30.', '172.31.')


def check_link(url: str) -> str:
    """Accept a public http(s) media link; refuse schemes and hosts yt-dlp
    could use to read this machine."""
    url = (url or '').strip()
    if not url:
        raise ValueError('Paste a video link first')
    parsed = urlsplit(url if '://' in url else 'https://' + url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError('Only http and https links are supported')
    host = (parsed.hostname or '').lower()
    if not host or host.startswith(LINK_HOSTS_BLOCKED):
        raise ValueError('That link does not point at a public video')
    return parsed.geturl()


def expand_short_link(url: str) -> str:
    """
    Follow a share link to its canonical address.

    Reddit's /s/ short links are the ones the app's share button produces, and
    yt-dlp has no extractor for them — it falls through to the generic one and
    Reddit answers 403. The canonical /comments/ URL works unauthenticated.
    """
    short = (re.search(r'reddit\.com/r/[^/]+/s/', url) or 'redd.it/' in url
             or re.search(r'(vt|vm)\.tiktok\.com/', url) or '/photo/' in url)
    if not short:
        return url
    try:
        # requests, not urllib: urllib does not use certifi here and fails
        # certificate verification before it ever sees the redirect.
        import requests
        with requests.get(url, headers={'User-Agent': BROWSER_UA}, timeout=20,
                          allow_redirects=True, stream=True) as resp:
            final = (resp.url or '').split('?')[0]
        if final and final != url:
            url = final
    except Exception:
        # Fall back to the original: yt-dlp's own error is clearer than ours.
        pass
    # TikTok serves photo posts under /photo/, which yt-dlp calls an
    # unsupported URL; the same id under /video/ is readable.
    return re.sub(r'(tiktok\.com/@[^/]+)/photo/', r'\1/video/', url)


def link_metadata(url: str) -> dict:
    """Ask yt-dlp what is behind a link, without downloading it."""
    url = expand_short_link(check_link(url))
    import agent
    ytdlp = agent.ytdlp_binary()
    if not ytdlp:
        raise ValueError('yt-dlp is not installed in this project')
    proc = subprocess.run(
        # TikTok in particular fails extraction intermittently ("universal data
        # for rehydration"); the same URL succeeds moments later.
        [ytdlp, '--no-warnings', '--skip-download', '--no-playlist',
         '--extractor-retries', '3', '--dump-single-json', url],
        capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        err = (proc.stderr or '').strip().splitlines()
        raise ValueError(err[-1][:300] if err else 'Could not read that link')
    data = json.loads(proc.stdout or '{}')
    if not any(f.get('vcodec') not in (None, 'none')
               for f in (data.get('formats') or [])):
        # No video track: a photo post. Count its slides so the caller can
        # build it as a slideshow rather than turning the user away.
        try:
            post = fetch_slide_post(url)
        except ValueError as exc:
            raise ValueError(f'That post has no video track. {exc}')
        return {
            'url': post['url'],
            'title': post['title'] or (data.get('title') or '').strip(),
            'uploader': post['uploader'] or (data.get('uploader') or '').strip(),
            'platform': (data.get('extractor_key') or 'Link').strip(),
            'duration': data.get('duration') or 0,
            'thumbnail': data.get('thumbnail') or '',
            'is_live': False,
            'is_slideshow': True,
            'slides': len(post['slides']),
        }
    return {
        'url': url,
        'title': (data.get('title') or '').strip(),
        'uploader': (data.get('uploader') or data.get('channel') or '').strip(),
        'platform': (data.get('extractor_key') or 'Link').strip(),
        'duration': data.get('duration') or 0,
        'thumbnail': data.get('thumbnail') or '',
        'is_live': bool(data.get('is_live')),
        'is_slideshow': False,
        'slides': 0,
    }


def youtube_title(headline: str, *fallbacks: str) -> str:
    """
    The title YouTube needs, which is not always the text on the card.

    A post whose media already carries its own text wants no on-screen
    headline, but the upload still needs a title and the library still needs
    a label — so fall back to the caption, then the source's own title.
    """
    for candidate in (headline, *fallbacks):
        text = (candidate or '').strip()
        if text:
            return text.title()[:90] + ' #shorts'
    return 'Miscellaneous Ken #shorts'


PRIVACY_CHOICES = ('private', 'unlisted', 'public')


def check_schedule(privacy: str, publish_at: str) -> tuple[str, str]:
    """
    Validate a per-video visibility and optional publish time.

    Returns (privacy, publish_at_rfc3339). A scheduled video must be uploaded
    private — that is YouTube's rule, not ours — so scheduling forces it.
    """
    privacy = (privacy or '').strip().lower()
    if privacy and privacy not in PRIVACY_CHOICES:
        raise ValueError('Visibility must be private, unlisted or public')
    publish_at = (publish_at or '').strip()
    if not publish_at:
        return privacy, ''
    try:
        when = dt.datetime.fromisoformat(publish_at.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Could not read that publish time')
    if when.tzinfo is None:
        when = when.astimezone()
    if when <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2):
        raise ValueError('Choose a publish time at least a couple of minutes from now')
    if when > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=365):
        raise ValueError('Choose a publish time within the next year')
    return 'private', when.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def draft_from_clip(clip: Path, cand, pick, framing: str, template: str,
                    description: str, move: bool = True,
                    max_seconds: float | None = None,
                    privacy: str = '', publish_at: str = '') -> dict:
    """Render a downloaded or uploaded clip into a draft. Shared by every
    hand-added video path so they cannot drift apart."""
    import render
    config = read_json(ROOT / 'config.json')
    apply_framing(config, framing)
    apply_template(config, template)
    fixed_track, fixed_start, credit = manual_track(config)

    out_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + cand.key
    out_path = ROOT / 'out' / f"{out_id}.mp4"
    out_path.parent.mkdir(exist_ok=True)
    poster_path = out_path.with_suffix('.png')
    workdir = ROOT / 'work'
    workdir.mkdir(exist_ok=True)

    with store.pipeline_lock():
        source = workdir / f"{cand.key}-source{clip.suffix}"
        if move:
            clip.replace(source)
        else:
            source = clip
        result = render.render_card(
            video=source,
            headline=pick.headline,
            out=out_path,
            config=config,
            max_seconds=max_seconds or config['posting']['max_clip_seconds'],
            poster=poster_path,
            workdir=workdir,
            music=fixed_track or None,
            music_start=fixed_start,
            mood=pick.mood,
            commentary=pick.caption,
        )
        pick.mood = result.mood
        draft_rec = store.draft(
            pick, result.output, poster_path, config,
            title=youtube_title(pick.headline, pick.caption,
                                (cand.title or ''), cand.source),
            description="\n\n".join(x for x in (description, credit) if x),
            source_clip=str(source),
            music=str(result.music) if result.music else fixed_track,
            music_start=fixed_start if fixed_track else 0.0,
            privacy=privacy, publish_at=publish_at,
        )
    return {'ok': True, 'id': draft_rec['id'], 'headline': draft_rec['headline'],
            'mood': draft_rec.get('mood', '')}


def delete_draft_files(record: dict) -> int:
    """
    Remove everything on disk that belongs to one draft.

    The rendered video in out/ was always cleaned up, but the downloaded
    source in work/ never was — and the source is the big file. Anything a
    *different* draft still points at is left alone: two imports of the same
    URL share a candidate key, so the source can be shared.
    """
    out_dir, work_dir = (ROOT / 'out').resolve(), (ROOT / 'work').resolve()
    keep: set[Path] = set()
    for other in store.records('videos'):
        if other['id'] == record['id']:
            continue
        for key in ('source_clip', 'video', 'poster'):
            if other.get(key):
                keep.add(Path(other[key]).resolve())
        for img in (other.get('images') or []):
            keep.add(Path(img).resolve())

    targets: set[Path] = set()
    for key in ('source_clip', 'video', 'poster'):
        if record.get(key):
            targets.add(Path(record[key]).resolve())
    for img in (record.get('images') or []):
        targets.add(Path(img).resolve())
    # Derived files are named after the draft id (overlays, posters of
    # re-renders) or the candidate key (source, frames, story images).
    for stem in (record['id'], record.get('candidate_key') or ''):
        if not stem:
            continue
        targets.update((ROOT / 'out').glob(f'{stem}*'))
        targets.update((ROOT / 'work').glob(f'{stem}*'))

    freed = 0
    for target in targets:
        target = Path(target).resolve()
        if target in keep or not target.is_file():
            continue
        if not (target.is_relative_to(out_dir) or target.is_relative_to(work_dir)):
            continue
        try:
            size = target.stat().st_size
            target.unlink()
            freed += size
        except OSError:
            pass
    return freed


SLIDE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.heic'}
MAX_SLIDE_BYTES = 40 * 1024**2
MAX_SLIDES = 12


def slideshow_dir(token: str) -> Path:
    """Staging folder for one slideshow upload. Token is caller-supplied, so
    it must not be able to escape work/."""
    if not re.fullmatch(r'[A-Za-z0-9]{8,40}', token or ''):
        raise ValueError('Invalid slideshow session')
    return ROOT / 'work' / f'slides-{token}'


def slide_audio(url: str, folder: Path) -> Path | None:
    """Download a photo post's original sound, if it has one."""
    import agent
    ytdlp = agent.ytdlp_binary()
    if not ytdlp:
        return None
    target = folder / 'sound.%(ext)s'
    subprocess.run(
        [ytdlp, '--no-warnings', '--no-playlist', '--extractor-retries', '3',
         '-f', 'ba/b', '-o', str(target), url],
        capture_output=True, text=True, timeout=300)
    # Trust the file, not the exit code: yt-dlp can write the audio and still
    # exit non-zero, which previously lost the sound's duration and left the
    # slideshow running for a default 2.5s over a 29-second track.
    found = [f for f in sorted(folder.glob('sound.*')) if f.stat().st_size > 0]
    return found[0] if found else None


def build_slideshow(token: str, seconds_each: float,
                    audio: Path | None = None) -> Path:
    """
    Turn staged stills into one clip so the normal render path can take it.

    Building a video here rather than a second renderer means a slideshow gets
    mood detection, the template choice, music and the length cap for free.
    """
    folder = slideshow_dir(token)
    slides = sorted(f for f in folder.iterdir()
                    if f.is_file() and f.suffix.lower() in SLIDE_EXTS)
    if not slides:
        raise ValueError('Add at least one image first')

    from PIL import Image
    # One common canvas: ffmpeg's concat demuxer needs every input the same
    # size, and mixed phone screenshots never are.
    width = max(Image.open(f).width for f in slides)
    height = max(Image.open(f).height for f in slides)
    width -= width % 2
    height -= height % 2
    staged = folder / 'normalised'
    staged.mkdir(exist_ok=True)
    for index, slide in enumerate(slides):
        with Image.open(slide) as im:
            im = im.convert('RGB')
            # Scale to fit the canvas in BOTH directions: thumbnail() only ever
            # shrinks, which left a small slide marooned in the middle of a
            # canvas sized by its larger siblings.
            scale = min(width / im.width, height / im.height)
            size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            im = im.resize(size, Image.LANCZOS)
            canvas = Image.new('RGB', (width, height), (0, 0, 0))
            canvas.paste(im, ((width - im.width) // 2, (height - im.height) // 2))
            canvas.save(staged / f'{index:03d}.png')

    # Every slide runs for the same time, so an image sequence at 1/duration
    # fps is the whole job: no concat list, no repeated final entry, and no
    # demuxer quirks (concat silently stopped after the first image here).
    out = folder / 'slideshow.mp4'
    cmd = ['ffmpeg', '-y', '-v', 'error',
           '-framerate', f'{1 / seconds_each:.6f}',
           '-i', str(staged / '%03d.png')]
    if audio and audio.is_file():
        # Muxing the post's own sound in here means composite() sees a clip
        # with audio and keeps it, exactly as it does for a downloaded video.
        cmd += ['-i', str(audio), '-c:a', 'aac', '-b:a', '160k', '-shortest']
    cmd += ['-r', '30', '-pix_fmt', 'yuv420p',
            '-c:v', 'libx264', '-preset', 'veryfast', str(out)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    return out


def find_key(node, key):
    """Walk a nested structure yielding every value under `key`."""
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for value in node.values():
            yield from find_key(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from find_key(value, key)


def fetch_slide_post(url: str) -> dict:
    """
    Read a TikTok photo post's slides out of its page.

    yt-dlp only exposes the cover image for these, so the slides come from
    TikTok's own embedded page state. That blob is internal and its shape
    changes without notice — every failure here should send the caller to the
    manual slideshow dialog rather than look like a bug.
    """
    import requests
    url = expand_short_link(check_link(url))
    # TikTok serves this page inconsistently — the same URL can come back
    # without the data blob and then with it moments later.
    html, match = '', None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers={'User-Agent': BROWSER_UA}, timeout=25)
            resp.raise_for_status()
            html = resp.text
        except Exception:
            html = ''
        if html:
            match = re.search(
                r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                html, re.S)
            if match:
                break
        if attempt < 2:
            time.sleep(2)
    if not html:
        raise ValueError('Could not open that post. Screenshot the slides and '
                         'use Slideshow from images instead.')
    if not match:
        raise ValueError('That page did not include the slide data — TikTok may '
                         'have changed it. Screenshot the slides and use '
                         'Slideshow from images instead.')
    try:
        data = json.loads(match.group(1))
    except ValueError:
        raise ValueError('The slide data could not be read. Screenshot the '
                         'slides and use Slideshow from images instead.')

    posts = list(find_key(data, 'imagePost'))
    images = (posts[0].get('images') or []) if posts else []
    urls: list[str] = []
    for image in images[:MAX_SLIDES]:
        candidates = (image.get('imageURL') or {}).get('urlList') or []
        if candidates:
            urls.append(candidates[0])
    if not urls:
        raise ValueError('No slides found — that looks like a video post, not a '
                         'photo slideshow. Use Import from link for videos.')

    # The handle is in the canonical URL; the page blob nests it under several
    # keys and one of them is literally the string "author".
    handle = re.search(r'tiktok\.com/@([^/?#]+)', url)
    author = handle.group(1) if handle else ''
    if not author:
        for value in find_key(data, 'author'):
            if isinstance(value, dict) and value.get('uniqueId'):
                author = value['uniqueId']
                break
    desc = next((d for d in find_key(data, 'desc') if isinstance(d, str) and d), '')
    return {'url': url, 'slides': urls, 'uploader': author, 'title': desc[:200]}


def import_slide_link(b: dict) -> dict:
    """Paste a photo-post link: fetch its slides, then build them as a slideshow."""
    import requests
    post = fetch_slide_post(b.get('url') or '')
    token = secrets.token_hex(8)
    folder = slideshow_dir(token)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        for index, slide in enumerate(post['slides']):
            try:
                resp = requests.get(slide, headers={'User-Agent': BROWSER_UA},
                                    timeout=30)
                resp.raise_for_status()
            except Exception:
                raise ValueError(f'Slide {index + 1} could not be downloaded. '
                                 'Screenshot the slides and use Slideshow from '
                                 'images instead.')
            if len(resp.content) > MAX_SLIDE_BYTES:
                raise ValueError(f'Slide {index + 1} is too large')
            (folder / f'{index:03d}.jpg').write_bytes(resp.content)
        sound = None
        if b.get('original_sound', True):
            sound = slide_audio(post['url'], folder)
        # One slide over a 29-second sound should hold for the sound, not for
        # the default 2.5s — match the pace to the audio unless told otherwise.
        if sound and not b.get('seconds_each'):
            import render
            length = render.probe_duration(sound)
            if length > 0:
                b = {**b, 'seconds_each': max(0.5, min(180, length / len(post['slides'])))}
        who = post['uploader']
        payload = {
            **b,
            'token': token,
            'headline': (b.get('headline') or ''),
            'source': f"tiktok:{who}" if who else 'tiktok',
            'credit': (b.get('credit')
                       or (f"Slides: @{who} on TikTok" if who else '')),
        }
        return import_slideshow(payload)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def import_slideshow(b: dict) -> dict:
    """Draft a slideshow from images already staged by /api/slideshow_add."""
    import agent
    headline = (b.get('headline') or '').strip().upper()
    if len(headline) > 90:
        raise ValueError('The on-screen headline must be 90 characters or fewer')
    caption = (b.get('caption') or '').strip()[:2000]
    framing = (b.get('framing') or 'auto').strip().lower()
    template = (b.get('template') or 'auto').strip().lower()
    if template not in ('auto', 'reaction_card', 'classic', 'caption_card'):
        raise ValueError('Invalid story template')
    try:
        each = float(b.get('seconds_each') or 2.5)
    except (TypeError, ValueError):
        raise ValueError('Seconds per slide must be a number')
    # Up to 60: with a single-image post the per-slide time is the whole clip,
    # so a 25-second request must not be clipped to a multi-slide bound.
    if not 0.5 <= each <= 180:
        raise ValueError('Seconds per slide must be between 0.5 and 180')

    token = (b.get('token') or '').strip()
    folder = slideshow_dir(token)
    if not folder.is_dir():
        raise ValueError('Those images are no longer staged — add them again')
    try:
        sound = folder / 'sound'
        found = sorted(folder.glob('sound.*'))
        clip = build_slideshow(token, each, found[0] if found else None)
        cand = agent.Candidate(
            title=headline,
            url='slideshow://' + secrets.token_hex(8),
            source=(b.get('source') or 'slideshow').strip()[:80],
            summary=caption,
            score=1000,
            published=dt.datetime.now(dt.timezone.utc).isoformat(),
            kind='video',
        )
        pick = agent.Pick(candidate=cand, headline=headline, caption=caption,
                          mood='neutral')
        credit = (b.get('credit') or '').strip()[:500]
        return draft_from_clip(
            clip, cand, pick, framing, template,
            "\n\n".join(x for x in (headline, caption, credit) if x))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def import_link(b: dict) -> dict:
    """Draft a video from any public link yt-dlp can read."""
    import agent
    url = expand_short_link(check_link(b.get('url') or ''))
    framing = (b.get('framing') or 'auto').strip().lower()
    template = (b.get('template') or 'auto').strip().lower()
    if template not in ('auto', 'reaction_card', 'classic', 'caption_card'):
        raise ValueError('Invalid story template')
    privacy, publish_at = check_schedule(b.get('privacy') or '', b.get('publish_at') or '')
    cap = b.get('max_seconds')
    if cap in (None, ''):
        cap = None
    else:
        try:
            cap = float(cap)
        except (TypeError, ValueError):
            raise ValueError('Clip length must be a number of seconds')
        if not 3 <= cap <= 180:
            raise ValueError('Clip length must be between 3 and 180 seconds')

    try:
        meta = link_metadata(url)
    except ValueError:
        # yt-dlp fails on TikTok intermittently, but the slideshow route reads
        # the page itself and needs none of it — try that before giving up.
        try:
            post = fetch_slide_post(url)
        except ValueError:
            raise
        # Only pin the pace when a clip length was asked for; left unset, the
        # slideshow paces itself to the post's own sound.
        extra = {'seconds_each': max(0.5, min(180, cap / max(1, len(post['slides']))))} if cap else {}
        return import_slide_link({**b, 'url': url, **extra})
    if meta['is_live']:
        raise ValueError('That link is a live stream, not a finished video')
    if meta.get('is_slideshow'):
        # Paste a photo post into the video importer and it still works: spread
        # the requested clip length across the slides, or fall back to a default.
        count = max(1, int(meta.get('slides') or 1))
        extra = {'seconds_each': max(0.5, min(180, cap / count))} if cap else {}
        return import_slide_link({**b, 'url': url, **extra})
    # Blank stays blank: the caller left it out on purpose because the clip
    # carries its own text. The YouTube title falls back separately.
    headline = (b.get('headline') or '').strip().upper()
    caption = (b.get('caption') or '').strip()
    who = meta['uploader']

    config = read_json(ROOT / 'config.json')
    workdir = ROOT / 'work'
    workdir.mkdir(exist_ok=True)
    clip, err = agent.download_clip(url, workdir,
                                    config['posting']['max_clip_seconds'],
                                    cookies_file=config['posting'].get('cookies_file', ''))
    if not clip:
        raise ValueError(f'Could not download that video: {err}')

    cand = agent.Candidate(
        title=headline,
        url=url,
        source=f"{meta['platform'].lower()}:{who}" if who else meta['platform'].lower(),
        summary=caption,
        score=1000,
        published=dt.datetime.now(dt.timezone.utc).isoformat(),
        kind='video',
    )
    pick = agent.Pick(candidate=cand, headline=headline, caption=caption, mood='neutral')
    credit_line = f"Source: {meta['platform']}"
    if who:
        credit_line += f" · {who}"
    credit_line += f"\nLink: {url}"
    return draft_from_clip(clip, cand, pick, framing, template,
                           "\n\n".join(x for x in (headline, caption, credit_line) if x),
                           max_seconds=cap, privacy=privacy, publish_at=publish_at)


def import_local_video(clip: Path, headline: str, caption: str, framing: str,
                       template: str = 'auto',
                       max_seconds: float | None = None,
                       privacy: str = '', publish_at: str = '') -> dict:
    """Draft a video already on this Mac. Mood and music come from the media."""
    import agent, render
    headline = headline.strip().upper()
    cand = agent.Candidate(
        title=headline,
        # Candidate.key hashes the url, and the source clip is stored under it.
        # Local files have no url, so give each upload its own — otherwise every
        # one collides and a later upload overwrites an earlier draft's source.
        url='local://' + secrets.token_hex(8),
        source='local:' + clip.name,
        summary=caption,
        score=1000,
        published=dt.datetime.now(dt.timezone.utc).isoformat(),
        kind='video',
    )
    pick = agent.Pick(candidate=cand, headline=headline, caption=caption, mood='neutral')
    return draft_from_clip(clip, cand, pick, framing, template,
                           "\n\n".join(x for x in (headline, caption) if x),
                           max_seconds=max_seconds, privacy=privacy,
                           publish_at=publish_at)


def import_reddit_post(b: dict) -> dict:
    url = (b.get('url') or '').strip()
    if not url:
        raise ValueError('Reddit post URL is required')

    info = inspect_reddit_url(url)
    sub = info['subreddit'] or 'reddit'
    cat = info['category'] or 'Reddit'

    headline = (b.get('headline') or '').strip().upper()
    caption = (b.get('caption') or info.get('caption') or '').strip()
    media_url = (b.get('media_url') or info.get('media_url') or info.get('video_url') or info.get('thumbnail_url') or '').strip()
    kind = (b.get('kind') or 'auto').strip()
    framing = (b.get('framing') or 'auto').strip().lower()
    template = (b.get('template') or 'auto').strip().lower()
    if template not in ('auto', 'reaction_card', 'classic', 'caption_card'):
        raise ValueError('Invalid story template')

    if kind == 'auto':
        if info.get('is_video') or (media_url and (media_url.lower().split('?')[0].endswith(('.mp4', '.mov', '.webm')) or 'v.redd.it' in media_url or 'packaged-media.redd.it' in media_url)):
            kind = 'video'
        else:
            kind = 'story'

    import agent
    cand = agent.Candidate(
        title=headline,
        url=url,
        source=f"reddit:{cat}:r/{sub}",
        summary=caption,
        score=1000,
        published=dt.datetime.now(dt.timezone.utc).isoformat(),
        kind=kind,
    )
    pick = agent.Pick(
        candidate=cand,
        headline=headline,
        caption=caption,
        mood='neutral',
    )

    config = read_json(ROOT / 'config.json')
    apply_framing(config, framing)
    apply_template(config, template)
    fixed_track, fixed_start, credit = manual_track(config)

    out_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + cand.key
    out_path = ROOT / 'out' / f"{out_id}.mp4"
    poster_path = out_path.with_suffix('.png')
    workdir = ROOT / 'work'
    workdir.mkdir(exist_ok=True)

    with store.pipeline_lock():
        if kind == 'story':
            import render_story
            from PIL import Image, ImageDraw
            img_paths = []
            img_urls = b.get('image_urls') or info.get('image_urls') or ([media_url] if media_url else [])
            for idx, u in enumerate(img_urls[:2]):
                try:
                    import urllib.request, ssl, certifi, io
                    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
                    img_path = workdir / f"{cand.key}-img{idx}.png"
                    req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'})
                    with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
                        img_data = resp.read()
                    im = Image.open(io.BytesIO(img_data)).convert('RGB')
                    im.save(img_path, 'PNG')
                    img_paths.append(img_path)
                except Exception:
                    pass

            real_images = bool(img_paths)
            if not img_paths:
                img_path = workdir / f"{cand.key}-card.png"
                im = Image.new('RGBA', (800, 600), (24, 26, 30, 255))
                d = ImageDraw.Draw(im)
                d.rectangle([20, 20, 780, 580], outline=(170, 242, 125, 255), width=3)
                d.text((400, 80), f"r/{sub.upper()}", fill=(170, 242, 125), anchor='mm')
                words = headline.split()
                lines = []
                cur = []
                for w in words:
                    cur.append(w)
                    if len(' '.join(cur)) > 32:
                        lines.append(' '.join(cur))
                        cur = []
                if cur:
                    lines.append(' '.join(cur))
                y = 180
                for l in lines[:5]:
                    d.text((400, y), l, fill=(240, 243, 246), anchor='mm')
                    y += 36
                if caption:
                    d.text((400, y + 36), f'"{caption[:100]}..."', fill=(161, 164, 176), anchor='mm')
                im.save(img_path)
                img_paths.append(img_path)

            # A generated placeholder card carries no mood worth reading.
            if real_images and not fixed_track:
                pick.mood = agent.analyze_visual_crop(
                    img_paths[0], config=config, workdir=workdir
                ).get('mood') or pick.mood
            music = fixed_track or agent.pick_music(pick.mood, config) or ''
            result = render_story.render_story(
                headline=pick.headline,
                commentary=pick.caption,
                images=img_paths,
                mascot=config.get('story', {}).get('mascot', 'assets/mascot.mp4'),
                out=out_path,
                config=config,
                duration=float(config.get('story', {}).get('duration_seconds', 12)),
                music=music,
                music_start=fixed_start,
                poster=poster_path,
                workdir=workdir,
            )
            title = youtube_title(headline, caption, info.get('title') or '',
                                  cand.source)
            desc = "\n\n".join(x for x in (headline, caption, f"Source: {cand.source}\nPost: {url}", credit) if x)
            draft_rec = store.draft(
                pick,
                result.output,
                poster_path,
                config,
                title=title,
                description=desc,
                template=result.template,
                images=[str(p) for p in img_paths],
                music=str(music) if music else '',
            )
            return {'ok': True, 'id': draft_rec['id'], 'headline': draft_rec['headline']}

        elif kind == 'video':
            import render
            clip, err = agent.download_clip(
                media_url or url,
                workdir,
                config['posting']['max_clip_seconds'],
            )
            if not clip:
                raise ValueError(f"Could not download video clip: {err}")
            # render_card reads the frame and the audio, so it picks the track.
            result = render.render_card(
                video=clip,
                headline=pick.headline,
                out=out_path,
                config=config,
                max_seconds=config['posting']['max_clip_seconds'],
                poster=poster_path,
                workdir=workdir,
                music=fixed_track or None,
                music_start=fixed_start,
                mood=pick.mood,
                commentary=caption,
            )
            pick.mood = result.mood
            music = result.music or fixed_track or ''
            title = youtube_title(headline, caption, info.get('title') or '',
                                  cand.source)
            desc = "\n\n".join(x for x in (headline, f"Source: {cand.source}\nPost: {url}", credit) if x)
            draft_rec = store.draft(
                pick,
                result.output,
                poster_path,
                config,
                title=title,
                description=desc,
                source_clip=str(clip),
                music=str(music) if music else '',
                music_start=fixed_start if fixed_track else 0.0,
            )
            return {'ok': True, 'id': draft_rec['id'], 'headline': draft_rec['headline']}
        else:
            raise ValueError("Unsupported format kind")

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def allowed(self):
        return self.headers.get('Host') in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')

    def send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self.read_request()
        except (sqlite3.Error, OSError):
            self.send(503, {'error': 'Local storage is unavailable. Free disk space and reopen the app.'})

    def read_request(self):
        if not self.allowed():
            return self.send(403,{'error':'Local access only'})
        path = urlsplit(self.path).path
        if path == '/api/status':
            videos = store.records('videos')
            public_videos = [{k:v for k,v in r.items() if k not in ('config','candidate','source_clip','images','music','video','poster')} for r in videos]
            for pv, r in zip(public_videos, videos):
                cand = r.get('candidate') or {}
                pv['source'] = cand.get('source', '')
                pv['source_url'] = cand.get('url', '')
                pv['kind'] = cand.get('kind', '')
            cfg = read_json(ROOT/'config.json')
            public_config = {section:{k:cfg.get(section,{}).get(k) for k in fields} for section,fields in FIELDS.items()}
            return self.send(200, dict(csrf=CSRF, online=ONLINE, busy=bool(ACTIVE) or store.busy(),
                automation=automation(), free_disk_bytes=shutil.disk_usage(ROOT).free, videos=public_videos, jobs=store.records('jobs')[:30], config=public_config,
                connections={name:bool(environment().get(key,'')) for name,key in KEYS.items()},
                youtube_connected=(ROOT/'token.json').exists(), oauth_client=(ROOT/'client_secret.json').exists()))
        elif path == '/api/link_inspect':
            params = parse_qs(urlsplit(self.path).query)
            url = (params.get('url') or [''])[0].strip()
            if not url: return self.send(400, {'error': 'URL required'})
            try:
                return self.send(200, link_metadata(url))
            except Exception as exc:
                return self.send(400, {'error': redact(str(exc))[-300:]})
        elif path == '/api/reddit_inspect':
            params = parse_qs(urlsplit(self.path).query)
            url = (params.get('url') or [''])[0].strip()
            if not url: return self.send(400, {'error': 'URL required'})
            try:
                return self.send(200, inspect_reddit_url(url))
            except Exception as exc:
                return self.send(400, {'error': str(exc)})
        elif path == '/api/media':
            params = parse_qs(urlsplit(self.path).query)
            record = store.get('videos', params.get('id',[''])[0])
            kind = params.get('kind',['video'])[0]
            if not record or kind not in ('video','poster'):
                return self.send(404,{'error':'Not found'})
            file = Path(record.get(kind,'')).resolve()
            if not file.is_relative_to((ROOT/'out').resolve()):
                return self.send(403,{'error':'Invalid media'})
        else:
            files = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if path not in files:
                return self.send(404,{'error':'Not found'})
            file = ROOT/'studio'/files[path]
        if not file.is_file():
            return self.send(404,{'error':'File unavailable'})
        size = file.stat().st_size
        start, end, status = 0, size-1, 200
        byte_range = self.headers.get('Range','')
        if byte_range:
            match = re.fullmatch(r'bytes=(\d+)-(\d*)',byte_range)
            if not match:
                return self.send(416,{'error':'Invalid range'})
            start=int(match[1]); end=min(int(match[2]) if match[2] else end,end)
            if start>end:
                return self.send(416,{'error':'Invalid range'})
            status=206
        self.send_response(status)
        self.send_header('Content-Type',mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(end-start+1))
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if status==206:
            self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        try:
            with file.open('rb') as f:
                f.seek(start); remaining=end-start+1
                while remaining:
                    chunk=f.read(min(65536,remaining))
                    if not chunk: break
                    self.wfile.write(chunk); remaining-=len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self.allowed() or self.headers.get('Origin') not in (f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}') or not secrets.compare_digest(self.headers.get('X-Studio-Token',''),CSRF):
            return self.send(403,{'error':'Reopen the app to reconnect securely'})
        # A video is orders of magnitude past the JSON body limit, so it
        # streams to disk on its own route instead of being parsed as a body.
        if urlsplit(self.path).path=='/api/upload_video':
            return self.receive_video()
        if urlsplit(self.path).path=='/api/slideshow_add':
            return self.receive_slide()
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=65536:
                raise ValueError('Request too large or empty')
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict): raise ValueError('Invalid request')
            with GUARD:
                result=self.mutate(urlsplit(self.path).path,body)
            self.send(200,result or {'ok':True})
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400,{'error':str(exc)})
        except Exception:
            self.send(500,{'error':'The action could not be completed. Check the local service log.'})

    def receive_slide(self):
        """Stage one image for a slideshow. Images are small, so no chunking."""
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_SLIDE_BYTES:
                raise ValueError(f'Each image must be under {MAX_SLIDE_BYTES//1024**2} MB')
            q=parse_qs(urlsplit(self.path).query)
            folder=slideshow_dir((q.get('token') or [''])[0].strip())
            name=(q.get('filename') or ['slide.png'])[0]
            ext=Path(name).suffix.lower()
            if ext not in SLIDE_EXTS:
                raise ValueError('Slides must be PNG, JPEG, WEBP or HEIC')
            folder.mkdir(parents=True, exist_ok=True)
            existing=[f for f in folder.iterdir() if f.suffix.lower() in SLIDE_EXTS]
            if len(existing)>=MAX_SLIDES:
                raise ValueError(f'A slideshow can hold up to {MAX_SLIDES} images')
            index=(q.get('index') or [str(len(existing))])[0]
            if not index.isdigit():
                raise ValueError('Invalid slide index')
            target=folder/f'{int(index):03d}{ext}'
            data=self.rfile.read(length)
            if len(data)!=length:
                raise ValueError('Upload ended early — try again')
            target.write_bytes(data)
            from PIL import Image
            try:
                with Image.open(target) as im: im.verify()
            except Exception:
                target.unlink(missing_ok=True)
                raise ValueError(f'{name} is not a readable image')
            self.send(200,{'ok':True,'staged':len(existing)+1})
        except (ValueError,KeyError,TypeError,OSError) as exc:
            self.send(400,{'error':str(exc)})
        except Exception:
            self.send(500,{'error':'That image could not be staged.'})

    def receive_video(self):
        tmp=None
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_UPLOAD_BYTES:
                raise ValueError(f'Choose a video up to {MAX_UPLOAD_BYTES//(1024**3)} GB')
            if shutil.disk_usage(ROOT).free < length + 2*1024**3:
                raise ValueError('Not enough free disk space for this video')
            if ACTIVE or store.busy():
                raise ValueError('Wait for the current job before adding a video')
            q=parse_qs(urlsplit(self.path).query)
            headline=(q.get('headline') or [''])[0].strip()
            if len(headline)>90:
                raise ValueError('The on-screen headline must be 90 characters or fewer')
            caption=(q.get('caption') or [''])[0].strip()[:2000]
            framing=(q.get('framing') or ['auto'])[0].strip().lower()
            template=(q.get('template') or ['auto'])[0].strip().lower()
            if template not in ('auto','reaction_card','classic','caption_card'):
                raise ValueError('Invalid story template')
            privacy, publish_at = check_schedule(
                (q.get('privacy') or [''])[0], (q.get('publish_at') or [''])[0])
            cap=(q.get('max_seconds') or [''])[0].strip()
            if cap:
                try: cap=float(cap)
                except ValueError: raise ValueError('Clip length must be a number of seconds')
                if not 3<=cap<=180: raise ValueError('Clip length must be between 3 and 180 seconds')
            else:
                cap=None
            # The name is used only for its extension; the file is renamed.
            ext=os.path.splitext((q.get('filename') or [''])[0])[1].lower()
            if ext not in ('.mp4','.mov','.m4v','.webm','.mkv'):
                raise ValueError('Choose an .mp4, .mov, .m4v, .webm or .mkv file')

            workdir=ROOT/'work'; workdir.mkdir(exist_ok=True)
            tmp=workdir/f"upload-{secrets.token_hex(8)}{ext}"
            remaining=length
            with open(tmp,'wb') as f:
                while remaining:
                    chunk=self.rfile.read(min(1024*1024,remaining))
                    if not chunk: raise ValueError('Upload ended early — try again')
                    f.write(chunk); remaining-=len(chunk)

            import render
            if not render.probe_video_dimensions(tmp)[1]:
                raise ValueError('That file has no readable video track')
            with GUARD:
                result=import_local_video(tmp,headline,caption,framing,template,cap,
                                          privacy,publish_at)
            tmp=None
            self.send(200,result)
        except (ValueError,KeyError,TypeError,OSError) as exc:
            self.send(400,{'error':str(exc)})
        except Exception:
            self.send(500,{'error':'The video could not be added. Check the local service log.'})
        finally:
            if tmp is not None:
                try: tmp.unlink()
                except OSError: pass

    def mutate(self,path,b):
        if path=='/api/jobs':
            return start_job(b['action'],b.get('id',''))
        if path=='/api/config':
            if ACTIVE or store.busy(): raise ValueError('Wait for the current job before changing settings')
            write_json(ROOT/'config.json',validate_config(b))
        elif path=='/api/automation':
            if set(b) != {'enabled','interval_hours','mode'}: raise ValueError('Invalid automation settings')
            if type(b.get('enabled')) is not bool or type(b.get('interval_hours')) not in (int,float) or not 1<=b['interval_hours']<=168 or b.get('mode') not in ('preview','upload'):
                raise ValueError('Choose an interval of 1–168 hours and a valid mode')
            old=automation()
            # Enabling starts when connectivity is available; interval edits restart the clock.
            next_run=old['next_run']
            if b['enabled'] and not old['enabled']: next_run=0
            elif b['interval_hours']!=old['interval_hours']: next_run=time.time()+b['interval_hours']*3600
            write_json(ROOT/'studio-settings.json',{**b,'next_run':next_run})
        elif path=='/api/keys':
            if ACTIVE: raise ValueError('Wait for the current job before changing connections')
            if b.get('provider') not in KEYS or not isinstance(b.get('value'),str) or len(b['value'])>4096:
                raise ValueError('Invalid API key')
            values=read_json(ROOT/'studio-secrets.json',{})
            values[KEYS[b['provider']]]=b['value'].strip()
            write_json(ROOT/'studio-secrets.json',values,private=True)
        elif path=='/api/oauth-client':
            data=b.get('client')
            if not isinstance(data,dict) or not isinstance(data.get('installed'),dict):
                raise ValueError('Choose a Google Desktop app OAuth JSON file')
            c=data['installed']
            if not c.get('client_id') or not c.get('client_secret') or c.get('auth_uri')!='https://accounts.google.com/o/oauth2/auth' or c.get('token_uri')!='https://oauth2.googleapis.com/token':
                raise ValueError('Use the original Desktop app JSON downloaded from Google Cloud')
            if ACTIVE: raise ValueError('Wait for the current job')
            write_json(ROOT/'client_secret.json',data,private=True)
        elif path=='/api/video':
            if ACTIVE or store.busy(): raise ValueError('Wait for the current job before modifying videos')
            r=store.get('videos',b['id'])
            if not r: raise ValueError('Video not found')
            if b.get('delete'):
                freed = delete_draft_files(r)
                store.delete('videos', r['id'])
                return {'ok': True, 'deleted': r['id'], 'freed_bytes': freed}
            if r['status']=='uploaded': raise ValueError('Only drafts can be edited')
            if b.get('check_upload'):
                if r['status'] not in ('upload_unknown', 'ready'):
                    raise ValueError('This draft does not need checking')
                import agent
                found = agent.find_recent_upload(r.get('title') or '', ROOT / 'state.json')
                if found:
                    r.update(status='uploaded', youtube_id=found,
                             uploaded_at=r.get('uploaded_at') or store.now(), error='')
                    store.put('videos', r['id'], r)
                    return {'ok': True, 'found': True, 'youtube_id': found}
                return {'ok': True, 'found': False}
            if b.get('reset_upload'):
                if r['status']!='upload_unknown': raise ValueError('This upload does not need reconciliation')
                r.update(status='ready',error='')
            else:
                if 'privacy' in b or 'publish_at' in b:
                    r['privacy'], r['publish_at'] = check_schedule(
                        b.get('privacy', r.get('privacy') or ''),
                        b.get('publish_at', r.get('publish_at') or ''))
                for key,limit in [('headline',90),('caption',2000),('title',100),('description',4900)]:
                    if key in b:
                        if not isinstance(b[key],str) or len(b[key])>limit: raise ValueError('Invalid '+key)
                        # The on-screen headline may be blank on purpose when
                        # the media carries its own text; the YouTube title
                        # cannot, because the upload needs one.
                        if key == 'title' and not b[key].strip(): raise ValueError('title cannot be empty')
                        if key in ('headline','caption') and b[key]!=r.get(key,''):
                            if r.get('legacy'): raise ValueError('Older videos only support title and description edits')
                            r['status']='needs_render'
                        r[key]=b[key]
            store.put('videos',r['id'],r)
        elif path=='/api/reddit_import':
            if ACTIVE or store.busy(): raise ValueError('Wait for the current job before importing')
            return import_reddit_post(b)
        elif path=='/api/link_import':
            if ACTIVE or store.busy(): raise ValueError('Wait for the current job before importing')
            return import_link(b)
        elif path=='/api/slideshow_import':
            if ACTIVE or store.busy(): raise ValueError('Wait for the current job before importing')
            return import_slideshow(b)
        elif path=='/api/slide_link_import':
            if ACTIVE or store.busy(): raise ValueError('Wait for the current job before importing')
            return import_slide_link(b)
        else:
            raise ValueError('Unknown action')

def main():
    global ACTIVE
    # Only one studio owns restart recovery and the scheduler.
    import fcntl
    lock=(ROOT/'.studio.lock').open('a')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: return 0
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    store.import_legacy()
    for job in store.records('jobs'):
        if job['status']=='running':
            job.update(status='interrupted',stage='Interrupted; inspect result before retrying',finished_at=store.now())
            store.put('jobs',job['id'],job)
    for r in store.records('videos'):
        if r['status'] in ('uploading','rendering') and not store.busy():
            r.update(status='upload_unknown' if r['status']=='uploading' else 'render_failed',error='The previous job was interrupted. Review before retrying.')
            store.put('videos',r['id'],r)
    threading.Thread(target=scheduler,daemon=True).start()
    print(f'Miscellaneous Ken Studio ready at http://127.0.0.1:{PORT}',flush=True)
    try: server.serve_forever()
    finally: STOP.set(); server.server_close()
    return 0

if __name__=='__main__':
    raise SystemExit(main())
