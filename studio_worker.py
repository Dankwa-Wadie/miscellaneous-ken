"""Isolated workers: each mutation shares the pipeline's process lock."""
import json
import sys
from pathlib import Path
import studio_store as store


def work(action, key):
    import agent
    store.load_secrets()
    with store.pipeline_lock():
        r = store.get('videos', key)
        if not r:
            raise ValueError('Video not found')
        if r['status'] == 'upload_unknown':
            raise ValueError('Check YouTube Studio and reconcile this upload before making changes')
        if r['status'] == 'uploaded':
            raise ValueError('This video has already been uploaded')
        config = json.loads((store.ROOT / 'config.json').read_text())
        if action == 'upload':
            if r['status'] != 'ready' or not r.get('title', '').strip():
                raise ValueError('Save a title and render pending edits before uploading')
            if r['status'] == 'upload_unknown':
                raise ValueError('Check YouTube Studio first, then mark this draft as not uploaded.')
            r.update(status='uploading', error='')
            store.put('videos', key, r)
            store.progress('Uploading')
            # A dropped connection can hide a completed upload, so look before
            # sending the file again — this is what turns a retry into a
            # duplicate on the channel.
            already = agent.find_recent_upload(r['title'], store.ROOT / 'state.json')
            if already:
                store.progress('Already on YouTube — not uploading again')
                vid = already
            else:
                try:
                    vid = agent.upload_youtube(Path(r['video']), r['title'], r['description'], config,
                        store.ROOT / 'token.json', privacy=r.get('privacy') or '',
                        publish_at=r.get('publish_at') or '',
                        report=lambda msg: store.progress(msg))
                    if not vid:
                        raise RuntimeError('No video ID returned')
                except Exception:
                    # One last look: the file may have landed even though the
                    # reply never arrived.
                    store.progress('Upload interrupted — checking YouTube')
                    landed = agent.find_recent_upload(r['title'], store.ROOT / 'state.json')
                    if landed:
                        vid = landed
                        store.progress('Upload had completed after all')
                    else:
                        r.update(status='upload_unknown',
                                 error='The connection dropped and we could not confirm the upload. '
                                       'Check YouTube Studio before retrying — do not upload again '
                                       'until you have looked.')
                        store.put('videos', key, r)
                        raise
            r.update(status='uploaded', youtube_id=vid, uploaded_at=store.now(),
                     privacy='private' if r.get('publish_at') else (r.get('privacy') or config['posting']['privacy']))
            store.put('videos', key, r)
            state = agent.State(store.ROOT / 'state.json')
            state.data['posted'][r['candidate_key']] = dict(url=r.get('candidate', {}).get('url', ''),
                headline=r['headline'], youtube_id=vid, dry_run=False, at=store.now())
            state.save()
        elif action == 'render':
            if r.get('legacy'):
                raise ValueError('This older video has no saved source details. Create a new preview to edit its appearance.')
            r.update(status='rendering', error='')
            store.put('videos', key, r)
            store.progress('Rendering')
            # Config is re-read so edited settings apply, but the template and
            # framing chosen when this draft was imported are part of the draft
            # itself — without this a re-render silently restyles it.
            saved = r.get('config') or {}
            for section, keys in (('story_layout', ('template', 'theme')),
                                  ('layout', ('video_fit', 'video_aspect',
                                              'background_anchor_y'))):
                for name in keys:
                    if name in (saved.get(section) or {}):
                        config.setdefault(section, {})[name] = saved[section][name]
            # Render to new files so a failed edit preserves the previous preview.
            output = store.ROOT / 'out' / (key + '-edit-' + __import__('uuid').uuid4().hex[:8] + '.mp4')
            poster = output.with_suffix('.png')
            try:
                if r['candidate']['kind'] == 'story':
                    import render_story
                    render_story.render_story(headline=r['headline'], commentary=r['caption'], images=r['images'],
                        mascot=config['story']['mascot'], out=output, poster=poster, config=config,
                        duration=config['story']['duration_seconds'], music=r.get('music') or None,
                        music_start=float(r.get('music_start') or 0), workdir=store.ROOT/'work')
                else:
                    import render
                    render.render_card(video=r['source_clip'], headline=r['headline'], out=output, poster=poster,
                        config=config, max_seconds=config['posting']['max_clip_seconds'],
                        music=r.get('music') or None, music_start=float(r.get('music_start') or 0),
                        commentary=r.get('caption') or '', workdir=store.ROOT/'work')
            except Exception:
                r.update(status='render_failed', error='Rendering failed. The previous preview is still available.')
                store.put('videos', key, r)
                raise
            r.update(status='ready', video=str(output), poster=str(poster), config=config, error='')
            store.put('videos', key, r)
        else:
            raise ValueError('Unknown action')

if __name__ == '__main__':
    work(*sys.argv[1:])
