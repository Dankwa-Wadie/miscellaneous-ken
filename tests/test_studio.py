import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import agent
import studio_store as store
import studio_server as server
import studio_worker as worker

class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.config=json.loads((store.ROOT/'config.json').read_text())
        self.config['notify']['enabled']=False
        (self.root/'config.json').write_text(json.dumps(self.config))
        (self.root/'out').mkdir();(self.root/'work').mkdir()
        self.stack=contextlib.ExitStack()
        for module,attr,value in [(store,'ROOT',self.root),(store,'DB',self.root/'test.sqlite3'),(server,'ROOT',self.root),(agent,'WORK_DIR',self.root/'work'),(agent,'OUT_DIR',self.root/'out')]:
            self.stack.enter_context(patch.object(module,attr,value))
        self.stack.enter_context(patch.object(server,'ACTIVE',None))
    def tearDown(self):
        self.stack.close();self.temp.cleanup()
    def run_pipeline(self,dry=True,fail_first=False):
        candidates=[agent.Candidate(title='A story '+str(i),url='https://example.com/'+str(i),source='test',video_url='https://example.com/'+str(i)) for i in range(3)]
        args=argparse.Namespace(config=str(self.root/'config.json'),state=str(self.root/'state.json'),token=str(self.root/'token.json'),no_llm=True,limit=1,dry_run=dry)
        def render(**kwargs):
            kwargs['out'].write_bytes(b'video');kwargs['poster'].write_bytes(b'poster')
            return argparse.Namespace(output=kwargs['out'],duration=12,mood=kwargs.get('mood','neutral'),music=None)
        results=[(None,'failed'),(self.root/'source.mp4','')] if fail_first else [(self.root/'source.mp4','')]*3
        with patch.object(agent,'discover',return_value=(candidates,[])),patch.object(agent,'download_clip',side_effect=results),patch.object(agent,'pick_music',return_value=None),patch.object(agent.render,'render_card',side_effect=render),patch.object(agent,'upload_youtube',return_value='yt-id'),patch.object(agent,'notify'),contextlib.redirect_stdout(io.StringIO()):
            agent.run(args)
        return json.loads((self.root/'state.json').read_text())
    def test_previews_are_drafts_not_posts(self):
        state=self.run_pipeline()
        self.assertEqual(state['posted'],{})
        self.assertEqual(state['runs'][-1]['posted'],0)
        self.assertEqual(state['runs'][-1]['rendered'],1)
        self.assertEqual(state['runs'][-1]['failed'],0)
        self.assertEqual(len(state['runs'][-1]['headlines']),1)
        record=store.records('videos')[0]
        self.assertEqual(record['status'],'ready')
        self.assertIn('Source:',record['description'])
    def test_actual_failures_exclude_unused_backups(self):
        state=self.run_pipeline(fail_first=True)
        self.assertEqual(state['runs'][-1]['failed'],1)
        self.assertEqual(state['runs'][-1]['headlines'],['A story 1'])
    def test_upload_updates_library_and_state(self):
        state=self.run_pipeline(dry=False)
        self.assertEqual(state['runs'][-1]['posted'],1)
        self.assertEqual(store.records('videos')[0]['youtube_id'],'yt-id')
    def test_draft_upload_and_duplicate_guard(self):
        self.run_pipeline()
        record=store.records('videos')[0]
        with patch.object(agent,'upload_youtube',return_value='new-id'),contextlib.redirect_stdout(io.StringIO()):
            worker.work('upload',record['id'])
        self.assertEqual(store.get('videos',record['id'])['status'],'uploaded')
        with self.assertRaises(ValueError): worker.work('upload',record['id'])
    def test_uncertain_upload_cannot_silently_retry(self):
        self.run_pipeline();record=store.records('videos')[0]
        with patch.object(agent,'upload_youtube',side_effect=RuntimeError('network lost')),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError): worker.work('upload',record['id'])
        self.assertEqual(store.get('videos',record['id'])['status'],'upload_unknown')
        with self.assertRaises(ValueError): worker.work('upload',record['id'])
        with self.assertRaises(ValueError): worker.work('render',record['id'])
    def test_edits_require_render(self):
        self.run_pipeline();r=store.records('videos')[0];r['status']='needs_render';store.put('videos',r['id'],r)
        with self.assertRaises(ValueError): worker.work('upload',r['id'])
    def test_settings_preserve_unexposed_fields(self):
        result=server.validate_config({'account':{'name':'Updated'}})
        self.assertEqual(result['discovery'],self.config['discovery'])
        self.assertEqual(result['layout'],self.config['layout'])
        with self.assertRaises(ValueError): server.validate_config({'posting':{'clips_per_run':0}})
        with self.assertRaises(ValueError): server.validate_config({'account':{'avatar':'/etc/passwd'}})
        with self.assertRaises(ValueError): server.validate_config({'editorial':{'models':{'openai':['a','b']}}})
    def test_lock_across_processes(self):
        with store.pipeline_lock():
            self.assertTrue(store.busy())
            code="import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
            proc=subprocess.run([sys.executable,'-c',code,str(self.root/'.pipeline.lock')],capture_output=True)
            self.assertNotEqual(proc.returncode,0)
        self.assertFalse(store.busy())
    def test_keys_private_and_redacted(self):
        key='test-secret-key-123'
        server.write_json(self.root/'studio-secrets.json',{'OPENAI_API_KEY':key},private=True)
        self.assertEqual((self.root/'studio-secrets.json').stat().st_mode&0o777,0o600)
        self.assertNotIn(key,server.redact('Error '+key))
    def test_legacy_migration_is_idempotent(self):
        (self.root/'state.json').write_text(json.dumps({'posted':{}}))
        (self.root/'out/old.mp4').write_bytes(b'old')
        (self.root/'out/old-edit-1.mp4').write_bytes(b'edit')
        store.import_legacy();store.import_legacy()
        self.assertEqual(len(store.records('videos')),1)
    def test_delete_video_removes_record_and_files(self):
        self.run_pipeline()
        r = store.records('videos')[0]
        v_file = Path(r['video'])
        p_file = Path(r['poster'])
        self.assertTrue(v_file.exists())
        self.assertTrue(p_file.exists())

        handler = server.Handler.__new__(server.Handler)
        res = handler.mutate('/api/video', {'id': r['id'], 'delete': True})
        self.assertTrue(res['ok'])
        self.assertEqual(res['deleted'], r['id'])

        self.assertIsNone(store.get('videos', r['id']))
        self.assertFalse(v_file.exists())
        self.assertFalse(p_file.exists())

        with self.assertRaises(ValueError):
            handler.mutate('/api/video', {'id': 'non-existent', 'delete': True})


class ServiceTests(unittest.TestCase):
    setUp = StudioTests.setUp
    tearDown = StudioTests.tearDown
    def test_http_local_security_and_media(self):
        import http.client
        import threading
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        port=httpd.server_address[1]
        with patch.object(server,'PORT',port):
            thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
            def request(method,path,body=None,headers=None):
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                conn.request(method,path,body,headers or {})
                response=conn.getresponse();data=response.read();status=response.status;conn.close()
                return status,data
            try:
                server.write_json(self.root/'studio-secrets.json',{'OPENAI_API_KEY':'never-return-this'},private=True)
                status,data=request('GET','/api/status')
                self.assertEqual(status,200);self.assertNotIn(b'never-return-this',data)
                status,_=request('GET','/api/status',headers={'Host':'attacker.example'})
                self.assertEqual(status,403)
                status,_=request('GET','/studio-secrets.json');self.assertEqual(status,404)
                status,_=request('POST','/api/automation','{}',{'Content-Type':'application/json'})
                self.assertEqual(status,403)
                headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}','X-Studio-Token':server.CSRF}
                status,_=request('POST','/api/automation',json.dumps({'enabled':False,'interval_hours':6,'mode':'preview'}),headers)
                self.assertEqual(status,200);self.assertEqual(server.automation()['interval_hours'],6)
                file=self.root/'out/test.mp4';file.write_bytes(b'0123456789')
                store.put('videos','test',{'id':'test','video':str(file)})
                status,data=request('GET','/api/media?id=test&kind=video',headers={'Range':'bytes=2-5'})
                self.assertEqual((status,data),(206,b'2345'))
            finally:
                httpd.shutdown();httpd.server_close();thread.join()
    def test_scheduler_waits_offline_then_runs_once(self):
        import threading
        stop=threading.Event()
        cfg={'enabled':True,'interval_hours':5,'mode':'preview','next_run':0}
        server.write_json(self.root/'studio-settings.json',cfg)
        ticks=[]
        def wait(_):
            ticks.append(1)
            if len(ticks)==2: stop.set()
        started=[]
        with patch.object(server,'STOP',stop),patch.object(stop,'wait',side_effect=wait),patch.object(server,'check_online',side_effect=[False,True]),patch.object(server.shutil,'disk_usage',return_value=argparse.Namespace(free=2**30)),patch.object(server,'start_job',side_effect=lambda *a,**k:started.append(a)):
            server.scheduler()
        self.assertEqual(started,[('preview',)])
        self.assertGreater(server.automation()['next_run'],0)
    def test_scheduler_waits_for_storage_without_consuming_slot(self):
        import threading
        stop=threading.Event()
        server.write_json(self.root/'studio-settings.json',{'enabled':True,'interval_hours':5,'mode':'upload','next_run':0})
        with patch.object(server,'STOP',stop),patch.object(stop,'wait',side_effect=lambda _:stop.set()),patch.object(server,'check_online',return_value=True),patch.object(server.shutil,'disk_usage',return_value=argparse.Namespace(free=100)),patch.object(server,'start_job') as start:
            server.scheduler()
        start.assert_not_called()
    def test_reddit_inspect_and_url_handling(self):
        class MockResp:
            def __init__(self, data): self.data = data
            def read(self): return json.dumps(self.data).encode()
            def __enter__(self): return self
            def __exit__(self, *a): pass

        with patch('urllib.request.urlopen', return_value=MockResp({'title': 'Quantum chip revealed', 'author_name': 'tech_user', 'thumbnail_url': 'https://example.com/thumb.jpg'})):
            info = server.inspect_reddit_url('https://www.reddit.com/r/technology/comments/abc123/quantum_chip_revealed/')
            self.assertEqual(info['subreddit'], 'technology')
            self.assertEqual(info['category'], 'Phones, Watches & Tech')
            self.assertEqual(info['title'], 'Quantum chip revealed')
            self.assertEqual(info['author'], 'tech_user')
            self.assertEqual(info['thumbnail_url'], 'https://example.com/thumb.jpg')

        # Test slug fallback when request fails
        with patch('urllib.request.urlopen', side_effect=Exception('offline')):
            info2 = server.inspect_reddit_url('https://www.reddit.com/r/technology/comments/abc123/quantum_chip_revealed/')
            self.assertEqual(info2['title'], 'Quantum chip revealed')

        with self.assertRaises(ValueError):
            server.inspect_reddit_url('https://example.com/not-reddit')

        import http.client, threading
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        port=httpd.server_address[1]
        with patch.object(server,'PORT',port):
            thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
            try:
                with patch('urllib.request.urlopen', return_value=MockResp({'title': 'Quantum chip revealed'})):
                    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                    conn.request('GET','/api/reddit_inspect?url=https%3A%2F%2Fwww.reddit.com%2Fr%2Ftechnology%2Fcomments%2Fabc123%2Fquantum_chip_revealed%2F')
                    resp=conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    data=json.loads(resp.read().decode())
                    self.assertEqual(data['subreddit'], 'technology')
                    conn.close()

                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                conn.request('GET','/api/reddit_inspect?url=')
                resp=conn.getresponse()
                self.assertEqual(resp.status, 400)
                conn.close()
            finally:
                httpd.shutdown();httpd.server_close();thread.join()

if __name__=='__main__': unittest.main()

