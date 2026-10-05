import datetime as dt
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer
from app.server import Service, Handler, DEFAULT_SETTINGS, configured_roots
from app.engine import MoveError

class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.roots={d:self.base/d for d in ('ssd1','drive1','drive2')}
        for r in self.roots.values():r.mkdir()
        self.service=Service(self.roots,self.base/'state',False,start_worker=False)
        self.service.save_settings({**self.service.settings(),'categories':['Anime','AnimeArc','Movies','Series'],'margin_gb':0,'age_days':60,'source':'ssd1','target':'drive1'})
        self.folder=self.service.engine.folder('ssd1','Anime','Frieren')
        self.folder.mkdir(parents=True)
        (self.folder/'episode').write_bytes(b'episode')
        old=dt.datetime.now().timestamp()-90*86400
        os.utime(self.folder/'episode',(old,old))
        self.req=dict(source='ssd1',category='Anime',name='Frieren',target='drive1')

    def tearDown(self):self.temp.cleanup()

class ServiceTests(ServiceFixture):
    def lock_request(self, desired):
        return dict(drive='ssd1', category='Anime', name='Frieren', locked=desired)

    def test_folder_lock_toggle_affects_inventory_and_policy(self):
        self.service.set_folder_lock(self.lock_request(True))
        row=next(r for r in self.service.engine.inventory() if r['name']=='Frieren')
        self.assertTrue(row['locked']);self.assertTrue(row['folder_locked']);self.assertFalse(row['parent_locked'])
        self.assertEqual(self.service.policy()['folders'],[])
        self.service.set_folder_lock(self.lock_request(False))
        self.assertFalse((self.folder/'.mvlock').exists())
        self.assertEqual(len(self.service.policy()['folders']),1)

    def test_lock_idempotent_preserves_existing_contents(self):
        lock=self.folder/'.mvlock';lock.write_text('existing annotation')
        self.service.set_folder_lock(self.lock_request(True))
        self.assertEqual(lock.read_text(),'existing annotation')
        self.service.set_folder_lock(self.lock_request(False))
        self.service.set_folder_lock(self.lock_request(False))
        self.assertFalse(lock.exists())

    def test_parent_lock_cannot_be_removed_by_folder_toggle(self):
        parent=self.folder.parent/'.mvlock';parent.write_text('parent lock')
        own=self.folder/'.mvlock';own.touch()
        with self.assertRaisesRegex(MoveError,'parent category'):
            self.service.set_folder_lock(self.lock_request(False))
        self.assertTrue(own.exists());self.assertEqual(parent.read_text(),'parent lock')
        row=next(r for r in self.service.engine.inventory() if r['name']=='Frieren')
        self.assertTrue(row['parent_locked'])

    def test_unsafe_lock_entries_refused(self):
        lock=self.folder/'.mvlock'
        for kind in ('symlink','directory','hardlink'):
            with self.subTest(kind=kind):
                if kind=='symlink':lock.symlink_to(self.folder/'episode')
                elif kind=='directory':lock.mkdir()
                else:os.link(self.folder/'episode',lock)
                try:
                    for desired in (True,False):
                        with self.assertRaisesRegex(MoveError,'Unsafe'):
                            self.service.set_folder_lock(self.lock_request(desired))
                finally:
                    if kind=='directory':lock.rmdir()
                    else:lock.unlink()
        self.assertEqual((self.folder/'episode').read_bytes(),b'episode')

    def test_toggle_refuses_active_job(self):
        self.service.enqueue(self.req)
        with self.assertRaisesRegex(MoveError,'active job'):
            self.service.set_folder_lock(self.lock_request(True))
        self.assertFalse((self.folder/'.mvlock').exists())

    def test_toggle_invalid_request_and_missing_folder(self):
        for changes in ({'locked':'false'},{'name':'../escape'},{'drive':'unknown'},{'name':'missing'}):
            with self.subTest(changes=changes):
                with self.assertRaises(MoveError):
                    self.service.set_folder_lock({**self.lock_request(True),**changes})

    def test_toggle_denied_permission_retains_lock_state(self):
        self.folder.chmod(0o555)
        try:
            with self.assertRaises(PermissionError):self.service.set_folder_lock(self.lock_request(True))
            self.assertFalse((self.folder/'.mvlock').exists())
        finally:self.folder.chmod(0o755)

    def test_toggle_invalidates_review_and_inventory_cache(self):
        review=self.service.review(self.req)
        self.service.state()
        self.service.set_folder_lock(self.lock_request(True))
        with self.assertRaisesRegex(MoveError,'expired'):
            self.service.enqueue_review(review['review_token'])
        row=next(r for r in self.service.state()['folders'] if r['name']=='Frieren')
        self.assertTrue(row['locked'])

    def test_review_then_queue_and_process(self):
        p=self.service.review(self.req)
        self.service.enqueue_review(p['review_token'])
        self.assertTrue(self.folder.exists())
        self.service.process_one()
        self.assertEqual(self.service.jobs()[0]['status'],'completed')
        self.assertFalse(self.folder.exists())

    def test_review_token_one_use(self):
        p=self.service.review(self.req)
        self.service.enqueue_review(p['review_token'])
        with self.assertRaisesRegex(MoveError,'expired'):self.service.enqueue_review(p['review_token'])

    def test_expired_review(self):
        p=self.service.review(self.req)
        _,plan=self.service.reviews[p['review_token']]
        self.service.reviews[p['review_token']]=(0,plan)
        with self.assertRaisesRegex(MoveError,'expired'):self.service.enqueue_review(p['review_token'])

    def test_no_duplicate_active_jobs(self):
        self.service.enqueue(self.req)
        with self.assertRaisesRegex(MoveError,'active job'):self.service.enqueue(self.req)

    def test_cancel_queued(self):
        j=self.service.enqueue(self.req)
        self.service.cancel(j['id'])
        self.assertFalse(self.service.process_one())
        self.assertTrue(self.folder.exists())
        self.assertEqual(self.service.jobs()[0]['status'],'cancelled')

    def test_failure_persists(self):
        self.service.enqueue(self.req)
        (self.folder/'episode').unlink();self.folder.rmdir()
        self.service.process_one()
        self.assertEqual(self.service.jobs()[0]['status'],'failed')
        self.assertIn('error',self.service.jobs()[0])

    def test_restart_marks_jobs_interrupted_no_auto_retry(self):
        self.service.enqueue(self.req)
        fresh=Service(self.roots,self.base/'state',False,start_worker=False)
        self.assertEqual(fresh.jobs()[0]['status'],'interrupted')
        self.assertFalse(fresh.process_one())

    def test_review_changes_before_worker_fail(self):
        p=self.service.review(self.req)
        self.service.enqueue_review(p['review_token'])
        (self.folder/'new').write_text('new')
        self.service.process_one()
        self.assertEqual(self.service.jobs()[0]['status'],'failed')
        self.assertTrue((self.folder/'episode').exists())

    def test_policy_age_lock_empty_other_drive(self):
        recent=self.service.engine.folder('ssd1','Anime','Recent');recent.mkdir();(recent/'e').write_text('new')
        empty=self.service.engine.folder('ssd1','Anime','Empty');empty.mkdir()
        self.assertEqual(len(self.service.policy()['folders']),1)
        (self.folder.parent/'.mvlock').touch()
        self.assertEqual(self.service.policy()['folders'],[])

    def test_schedule_off_no_jobs(self):
        now=dt.datetime(2026,10,5,3,0,tzinfo=ZoneInfo('Europe/Dublin'))
        self.service.scheduler_tick(now)
        self.assertEqual(self.service.jobs(),[])
        self.assertIsNone(self.service.next_run())

    def test_schedule_once_in_scheduled_minute(self):
        self.service.save_settings({**self.service.settings(),'enabled':True})
        now=dt.datetime(2026,10,5,3,0,tzinfo=ZoneInfo('Europe/Dublin'))
        self.service.scheduler_tick(now);self.service.scheduler_tick(now)
        self.assertEqual(len(self.service.jobs()),1)

    def test_missed_runs_skipped(self):
        self.service.save_settings({**self.service.settings(),'enabled':True})
        self.service.scheduler_tick(dt.datetime(2026,10,5,3,1,tzinfo=ZoneInfo('Europe/Dublin')))
        self.assertEqual(self.service.jobs(),[])

    def test_weekday_monthly_and_dst_matching(self):
        settings=self.service.settings()
        settings.update(frequency='weekdays',weekdays=[0])
        self.assertTrue(Service.matches(settings,dt.date(2026,10,5)))
        self.assertFalse(Service.matches(settings,dt.date(2026,10,6)))
        settings.update(frequency='monthly',day=4)
        self.assertTrue(Service.matches(settings,dt.date(2026,10,4)))
        self.assertFalse(Service.matches(settings,dt.date(2026,10,5)))
        # Repeated DST minute must enqueue at most once per local day.
        self.service.save_settings({**self.service.settings(),'enabled':True,'time':'01:30'})
        self.service.scheduler_tick(dt.datetime(2026,10,25,1,30,tzinfo=ZoneInfo('Europe/Dublin'),fold=0))
        self.service.scheduler_tick(dt.datetime(2026,10,25,1,30,tzinfo=ZoneInfo('Europe/Dublin'),fold=1))
        self.assertEqual(len(self.service.jobs()),1)

    def test_invalid_settings(self):
        for key,val in [('enabled','yes'),('time','25:00'),('timezone','Invalid/Zone'),('frequency','hourly'),('age_days',-1),('margin_gb',True),('max_folders',0),('day',31),('categories',['../escape']),('weekdays',[7]),('target','unknown'),('source','unknown')]:
            with self.subTest(key=key):
                with self.assertRaises(MoveError):self.service.save_settings({**self.service.settings(),key:val})

    def test_plan_preview_does_not_enqueue(self):
        self.service.policy()
        self.assertEqual(self.service.jobs(),[])
        self.assertTrue(self.folder.exists())

    def test_max_folders_and_max_bytes(self):
        for name in ('A','B'):
            p=self.service.engine.folder('ssd1','Anime',name);p.mkdir();(p/'e').write_text('old');os.utime(p/'e',(0,0))
        self.service.save_settings({**self.service.settings(),'max_folders':1})
        self.assertEqual(len(self.service.policy()['folders']),1)

    def test_scheduling_all_six_explicit_routes(self):
        for source in self.roots:
            for target in self.roots:
                if source==target:continue
                with self.subTest(source=source,target=target):
                    name=source+'-'+target
                    folder=self.service.engine.folder(source,'Movies',name)
                    folder.mkdir(parents=True);(folder/'e').write_bytes(b'test')
                    self.service.save_settings({**self.service.settings(),'source':source,'target':target,'age_days':0,'categories':['Movies']})
                    preview=self.service.policy()
                    self.assertEqual(len(preview['folders']),1)
                    self.assertEqual(preview['folders'][0]['source'],source)
                    self.assertEqual(preview['folders'][0]['target'],target)
                    self.service.policy(execute=True)
                    self.service.process_one()
                    self.assertEqual(self.service.jobs()[0]['status'],'completed')
                    folder=self.service.engine.folder(target,'Movies',name)
                    (folder/'e').unlink();folder.rmdir()

    def test_blank_schedule_requires_explicit_route(self):
        self.service.save_settings({**self.service.settings(),'source':'','target':''})
        with self.assertRaisesRegex(MoveError,'source location'):self.service.policy()
        with self.assertRaisesRegex(MoveError,'source location'):
            self.service.save_settings({**self.service.settings(),'enabled':True})
        self.service.save_settings({**self.service.settings(),'source':'drive1','target':''})
        with self.assertRaisesRegex(MoveError,'destination location'):self.service.policy()

    def test_same_location_schedule_rejected(self):
        with self.assertRaisesRegex(MoveError,'different locations'):
            self.service.save_settings({**self.service.settings(),'source':'drive2','target':'drive2'})

    def test_legacy_auto_schedule_migration_disables_without_guessing(self):
        legacy={k:v for k,v in self.service.settings().items() if k!='source'}
        legacy.update(enabled=True,target='auto')
        with self.service.db() as db:
            db.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(legacy),))
        fresh=Service(self.roots,self.base/'state',False,start_worker=False)
        settings=fresh.settings()
        self.assertFalse(settings['enabled'])
        self.assertEqual(settings['source'],'');self.assertEqual(settings['target'],'auto')
        self.assertTrue(all(role=='unassigned' for role in settings['location_roles'].values()))
        self.assertIsNone(fresh.next_run())

    def test_generic_locations_schedule(self):
        service=Service({'Left':self.roots['drive1'],'Right':self.roots['ssd1']},self.base/'generic-state',False,start_worker=False)
        folder=service.engine.folder('Left','Anime','Generic');folder.mkdir(parents=True);(folder/'e').write_text('data')
        service.save_settings({**service.settings(),'source':'Left','target':'Right','age_days':0,'margin_gb':0})
        self.assertEqual(service.policy()['folders'][0]['target'],'Right')

    def test_user_roles_control_automatic_schedule(self):
        settings={**self.service.settings(),'target':'auto','location_roles':{'ssd1':'cache','drive1':'cold','drive2':'unassigned'}}
        self.service.save_settings(settings)
        self.assertEqual(self.service.policy()['folders'][0]['target'],'drive1')
        settings['location_roles']['drive1']='cache'
        self.service.save_settings(settings)
        with self.assertRaisesRegex(MoveError,'Assign another location'):self.service.policy()
        with self.assertRaisesRegex(MoveError,'Assign another location'):
            self.service.save_settings({**settings,'enabled':True})

    def test_invalid_location_roles_rejected(self):
        for roles in ({}, {'ssd1':'cache'}, {**self.service.settings()['location_roles'],'drive1':'warm'}, []):
            with self.subTest(roles=roles):
                with self.assertRaisesRegex(MoveError,'valid role'):
                    self.service.save_settings({**self.service.settings(),'location_roles':roles})

    def test_role_changes_do_not_override_explicit_route(self):
        self.service.save_settings({**self.service.settings(),'location_roles':{name:'cache' for name in self.roots}})
        self.assertEqual(self.service.policy()['folders'][0]['target'],'drive1')

    def test_configured_location_map(self):
        from unittest.mock import patch
        mapping={'Left':str(self.roots['drive1']),'Right':str(self.roots['ssd1'])}
        with patch.dict(os.environ,{'MOVER_ROOTS':json.dumps(mapping)}):
            self.assertEqual(configured_roots(),mapping)
        for invalid in ('[]','{}','{"A":"relative","B":"/tmp"}','garbage'):
            with patch.dict(os.environ,{'MOVER_ROOTS':invalid}):
                with self.assertRaises(MoveError):configured_roots()

    def test_boolean_lock_override_required(self):
        with self.assertRaisesRegex(MoveError,'boolean'):self.service.review({**self.req,'override_lock':'false'})

class HTTPTests(ServiceFixture):
    def setUp(self):
        super().setUp()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.service=self.service;self.server.token='test-token'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();super().tearDown()

    def request(self,path,body=None,headers=None,raw=None):
        conn=http.client.HTTPConnection(*self.server.server_address)
        h={'Authorization':'Bearer test-token','Content-Type':'application/json'}
        h.update(headers or {})
        conn.request('POST' if body is not None or raw is not None else 'GET',path,body=json.dumps(body) if raw is None and body is not None else raw,headers=h)
        r=conn.getresponse();result=(r.status,r.read(),dict(r.getheaders()));conn.close();return result

    def test_run_now_requires_review_and_keeps_reviewed_selection(self):
        status, raw, _ = self.request('/api/preview', {'id':'default'})
        self.assertEqual(status,200)
        review=json.loads(raw)
        self.assertTrue(review['review_token'])
        self.assertEqual(self.request('/api/run-now',{'id':'default'})[0],400)
        fresh=self.service.engine.folder('ssd1','Anime','Added later')
        fresh.mkdir(parents=True);(fresh/'episode').write_bytes(b'new')
        status, raw, _ = self.request('/api/run-now', {'id':'default','review_token':review['review_token']})
        self.assertEqual(status,200)
        self.assertEqual([p['name'] for p in json.loads(raw)['folders']],['Frieren'])
        self.assertEqual(self.request('/api/run-now', {'id':'default','review_token':review['review_token']})[0],400)

    def test_lock_endpoint_requires_auth_and_updates_folder(self):
        request=dict(drive='ssd1',category='Anime',name='Frieren',locked=True)
        self.assertEqual(self.request('/api/lock',request,headers={'Authorization':''})[0],401)
        self.assertFalse((self.folder/'.mvlock').exists())
        self.assertEqual(self.request('/api/lock',request)[0],200)
        self.assertTrue((self.folder/'.mvlock').exists())
        self.assertEqual(self.request('/api/lock',{**request,'locked':False})[0],200)
        self.assertFalse((self.folder/'.mvlock').exists())

    def test_unauthenticated_api_refused(self):
        self.assertEqual(self.request('/api/state',headers={'Authorization':''})[0],401)
        self.assertEqual(self.request('/api/run-now',{},headers={'Authorization':''})[0],401)

    def test_cross_origin_write_refused(self):
        self.assertEqual(self.request('/api/run-now',{},headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.service.jobs(),[])

    def test_non_json_write_refused(self):
        self.assertEqual(self.request('/api/run-now',{},headers={'Content-Type':'text/plain'})[0],415)

    def test_malformed_and_invalid_body(self):
        for raw in ('{','[]','null','"hello"'):
            self.assertEqual(self.request('/api/review',raw=raw)[0],400)

    def test_http_review_confirmation_and_worker(self):
        status,raw,_=self.request('/api/review',self.req)
        self.assertEqual(status,200)
        token=json.loads(raw)['review_token']
        self.assertEqual(self.request('/api/move',{'review_token':token})[0],200)
        self.service.process_one()
        self.assertEqual(self.service.jobs()[0]['status'],'completed')

    def test_static_and_health(self):
        for path in ('/','/app.js','/style.css','/health'):
            self.assertEqual(self.request(path,headers={'Authorization':''})[0],200)
        self.assertEqual(self.request('/../../etc/passwd')[0],404)

    def test_state_and_missing_folder_error(self):
        self.assertEqual(self.request('/api/state')[0],200)
        self.assertEqual(self.request('/api/review',{**self.req,'name':'missing'})[0],400)
