"""Durable library and cross-process coordination for the local studio."""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'studio.sqlite3'

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

@contextlib.contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    # Default rollback journaling keeps reads free of shared-memory files.
    db.execute('CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    try:
        with db:
            yield db
    finally:
        db.close()

def put(table, key, data):
    assert table in ('videos', 'jobs')
    with connect() as db:
        db.execute(f'INSERT INTO {table} VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (key, json.dumps(data)))

def get(table, key):
    assert table in ('videos', 'jobs')
    if not DB.exists():
        return None
    with contextlib.closing(sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(f'SELECT data FROM {table} WHERE id=?', (key,)).fetchone()
    return json.loads(row['data']) if row else None

def records(table):
    assert table in ('videos', 'jobs')
    if not DB.exists():
        return []
    with contextlib.closing(sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f'SELECT data FROM {table} ORDER BY rowid DESC').fetchall()
    return [json.loads(r['data']) for r in rows]

def delete(table, key):
    assert table in ('videos', 'jobs')
    if not DB.exists():
        return
    with connect() as db:
        db.execute(f'DELETE FROM {table} WHERE id=?', (key,))

@contextlib.contextmanager
def pipeline_lock():
    with (ROOT / '.pipeline.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another pipeline job is running. Try again after it finishes.')
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

def busy():
    try:
        with pipeline_lock():
            return False
    except RuntimeError:
        return True

def load_secrets():
    path = ROOT / 'studio-secrets.json'
    if path.exists():
        for key, value in json.loads(path.read_text()).items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

def draft(pick, video, poster, config, title, description, **extras):
    record = dict(id=video.stem, candidate_key=pick.candidate.key, created_at=now(),
                  status='ready', video=str(video), poster=str(poster), headline=pick.headline,
                  caption=pick.caption, title=title, description=description, mood=pick.mood,
                  candidate=__import__('dataclasses').asdict(pick.candidate),
                  config=config, youtube_id='', error='', **extras)
    put('videos', record['id'], record)
    return record

def progress(stage, **values):
    print('MKEN_PROGRESS ' + json.dumps(dict(stage=stage, **values)), flush=True)

def import_legacy():
    """Index existing artifacts without inventing missing editorial metadata."""
    state_path = ROOT / 'state.json'
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text())
    indexed = {r.get('video') for r in records('videos')}
    paths = sorted(p for p in (ROOT / 'out').glob('*.mp4') if '-edit' not in p.stem)
    latest = {p.stem.rsplit('-', 1)[-1]: p for p in paths}
    for video in paths:
        if str(video) in indexed or get('videos', video.stem):
            continue
        key = video.stem.rsplit('-', 1)[-1]
        old = state.get('posted', {}).get(key, {})
        uploaded = bool(old.get('youtube_id')) and not old.get('dry_run') and latest[key] == video
        put('videos', video.stem, dict(id=video.stem, candidate_key=key,
            created_at=dt.datetime.fromtimestamp(video.stat().st_mtime, dt.timezone.utc).isoformat(),
            video=str(video), poster=str(video.with_suffix('.png')), headline=old.get('headline', video.stem),
            title=old.get('headline', ''), caption='', description=('Source: '+old['url']) if old.get('url') else '',
            status='uploaded' if uploaded else 'ready', youtube_id=old.get('youtube_id', '') if uploaded else '',
            legacy=True, error=''))
