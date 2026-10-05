import datetime as dt
import http.client
import json
import os
import shutil
import threading
from unittest.mock import patch
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer
from test_service import ServiceFixture
from app.server import Service
from app.engine import MoveError

class FeatureTests(ServiceFixture):
    def second(self,name='Second',category='Movies',drive='ssd1'):
        p=self.service.engine.folder(drive,category,name)
        p.mkdir(parents=True);(p/'episode').write_bytes(b'data');os.utime(p/'episode',(0,0))
        return dict(source=drive,category=category,name=name,target='drive1')

    def schedule_data(self,name='Route',**changes):
        return {**{k:v for k,v in self.service.settings().items() if k!='location_roles'},'id':None,'name':name,**changes}

    def test_batch_all_jobs_queued_atomically_and_completed(self):
        request=self.second()
        review=self.service.review_batch([self.req,request])
        result=self.service.enqueue_review(review['review_token'])
        self.assertEqual(len(result['jobs']),2)
        self.assertTrue(self.service.process_one());self.assertTrue(self.service.process_one())
        task=self.service.task_detail(result['id'])
        self.assertEqual(task['task']['status'],'completed')
        self.assertEqual(len(task['children']),2)
        self.assertTrue(all(t['status']=='completed' for t in task['children']))

    def test_batch_error_blocks_entire_batch(self):
        other=self.second()
        destination=self.service.engine.folder('drive1','Anime','Frieren')
        destination.mkdir(parents=True);(destination/'episode').write_bytes(b'conflict')
        review=self.service.review_batch([self.req,other])
        self.assertIsNone(review['review_token']);self.assertTrue(review['errors'])
        self.assertEqual(self.service.jobs(),[])
        self.assertFalse(self.service.engine.folder('drive1','Movies','Second').exists())
        detail=self.service.task_detail(review['task_id'])
        self.assertEqual(detail['task']['status'],'failed')
        self.assertTrue(any(e['level']=='error' for e in detail['events']))

    def test_batch_collisions_between_selected_items(self):
        other=self.second(name='Frieren',category='Movies')
        requests=[{**self.req,'destination_category':'Series'},{**other,'destination_category':'Series'}]
        review=self.service.review_batch(requests)
        self.assertIsNone(review['review_token'])
        self.assertTrue(any('Batch destination collision' in e['error'] for e in review['errors']))

    def test_batch_file_directory_collision_between_selected_items(self):
        other=self.second(name='Frieren',category='Movies')
        p=self.service.engine.folder('ssd1','Movies','Frieren')
        (p/'episode').unlink();(p/'episode').mkdir();(p/'episode/e').write_bytes(b'other')
        review=self.service.review_batch([{**self.req,'destination_category':'Series'},{**other,'destination_category':'Series'}])
        self.assertIsNone(review['review_token'])

    def test_batch_cannot_write_into_another_selected_source(self):
        other=self.second(name='Frieren',category='Anime',drive='drive1')
        p=self.service.engine.folder('drive1','Anime','Frieren');(p/'episode').rename(p/'different')
        other['target']='drive2'
        review=self.service.review_batch([self.req,other])
        self.assertTrue(any('another selected source' in e['error'] for e in review['errors']))

    def test_batch_atomic_rollback_when_item_already_queued(self):
        other=self.second()
        review=self.service.review_batch([self.req,other])
        self.service.enqueue(other)
        with self.assertRaisesRegex(MoveError,'active job'):
            self.service.enqueue_review(review['review_token'])
        self.assertEqual(len(self.service.jobs()),1)
        batches=self.service.tasks(kind='batch')['tasks']
        self.assertEqual(len(batches),1);self.assertEqual(batches[0]['status'],'failed')

    def test_batch_source_changes_before_confirmation_rejects_all(self):
        other=self.second()
        review=self.service.review_batch([self.req,other])
        (self.folder/'new').write_text('new')
        with self.assertRaisesRegex(MoveError,'since batch review'):
            self.service.enqueue_review(review['review_token'])
        self.assertEqual(self.service.jobs(),[])

    def test_batch_duplicate_and_invalid_items(self):
        for requests in ([],[self.req,self.req],[dict(name='unknown')]):
            if not requests:
                with self.assertRaises(MoveError):self.service.review_batch(requests)
            else:self.assertIsNone(self.service.review_batch(requests)['review_token'])

    def test_batch_aggregate_free_space(self):
        other=self.second()
        # Each item fits independently, but their total does not.
        with patch('app.engine.shutil.disk_usage',return_value=shutil._ntuple_diskusage(100,90,10)):
            review=self.service.review_batch([self.req,other])
        self.assertTrue(any('complete batch' in e['error'] for e in review['errors']))

    def test_batch_lock_reports_each_failure_without_removing_parent_lock(self):
        other=self.second()
        (self.folder.parent/'.mvlock').touch()
        result=self.service.set_batch_locks(dict(locked=True,items=[dict(drive='ssd1',category='Movies',name=other['name'])]))
        self.assertEqual(len(result['changed']),1)
        result=self.service.set_batch_locks(dict(locked=False,items=[dict(drive='ssd1',category='Anime',name='Frieren'),dict(drive='ssd1',category='Movies',name=other['name'])]))
        self.assertEqual(len(result['errors']),1);self.assertEqual(len(result['changed']),1)
        self.assertTrue((self.folder.parent/'.mvlock').exists())
        self.assertFalse((self.service.engine.folder('ssd1','Movies','Second')/'.mvlock').exists())
        self.assertEqual(self.service.task_detail(result['task_id'])['task']['status'],'failed')

    def test_move_timeline_includes_per_file_events(self):
        job=self.service.enqueue(self.req);self.service.process_one()
        detail=self.service.task_detail(job['id'])
        messages=[e['message'] for e in detail['events']]
        for message in ('Move queued','File copied and published','File content verified','Source file removed','Move completed'):
            self.assertIn(message,messages)
        self.assertEqual(detail['task']['status'],'completed')
        self.assertNotIn('reviewed_snapshot',detail['task']['request'])

    def test_failed_review_is_logged_without_source_changes(self):
        with self.assertRaises(MoveError):self.service.review({**self.req,'target':'ssd1'})
        task=self.service.tasks(kind='review',status='failed')['tasks'][0]
        detail=self.service.task_detail(task['id'])
        self.assertEqual(detail['events'][0]['level'],'error')
        self.assertTrue((self.folder/'episode').exists())

    def test_failed_move_exception_type_and_context_persist(self):
        job=self.service.enqueue(self.req)
        with patch.object(self.service.engine,'move',side_effect=PermissionError('disk denied')):self.service.process_one()
        detail=self.service.task_detail(job['id'])
        self.assertEqual(detail['task']['details']['error_type'],'PermissionError')
        self.assertEqual(detail['task']['details']['error'],'disk denied')
        self.assertTrue(any(e['level']=='error' for e in detail['events']))

    def test_log_and_event_pagination(self):
        task=self.service.create_task('review','Search me',dict(source='ssd1'),status='completed')
        for i in range(12):self.service.event(task,'info',f'Event {i}')
        first=self.service.task_detail(task,limit=5)
        second=self.service.task_detail(task,after=first['events'][-1]['seq'],limit=10)
        self.assertEqual(len(first['events']),5);self.assertTrue(first['has_more'])
        self.assertEqual(len(second['events']),7);self.assertFalse(second['has_more'])
        for i in range(4):self.service.create_task('review',f'Task {i}',{},status='failed')
        page=self.service.tasks(limit=2);page2=self.service.tasks(offset=2,limit=2)
        self.assertEqual(len(page['tasks']),2);self.assertTrue(page['has_more'])
        self.assertFalse(set(t['id'] for t in page['tasks'])&set(t['id'] for t in page2['tasks']))
        self.assertEqual(len(self.service.tasks(search='search me')['tasks']),1)
        self.assertEqual(self.service.tasks(status='failed')['total'],4)

    def test_log_persists_and_active_job_interruption_preserves_context(self):
        job=self.service.enqueue(self.req)
        self.service.update(job['id'],'running',current='episode',completed_bytes=3)
        self.service.event(job['id'],'info','Before crash')
        fresh=Service(self.roots,self.base/'state',False,start_worker=False)
        detail=fresh.task_detail(job['id'])
        self.assertEqual(detail['task']['status'],'interrupted')
        self.assertEqual(detail['task']['details']['completed_bytes'],3)
        self.assertTrue(any('restarted' in e['message'] for e in detail['events']))

    def test_schedules_are_independent_and_fire_same_minute(self):
        one=self.service.save_schedule(self.schedule_data('One',enabled=True))
        self.second(drive='drive2')
        two=self.service.save_schedule(self.schedule_data('Two',source='drive2',enabled=True))
        now=dt.datetime(2026,10,5,3,0,tzinfo=ZoneInfo('Europe/Dublin'))
        self.service.scheduler_tick(now);self.service.scheduler_tick(now)
        self.assertEqual(len(self.service.jobs()),2)
        self.assertEqual({j['request']['schedule_id'] for j in self.service.jobs()},{one['id'],two['id']})
        self.assertEqual(self.service.tasks(kind='schedule')['total'],2)

    def test_overlapping_schedules_log_duplicate_queue_error(self):
        self.service.save_schedule(self.schedule_data('One',enabled=True))
        self.service.save_schedule(self.schedule_data('Two',enabled=True))
        self.service.scheduler_tick(dt.datetime(2026,10,5,3,0,tzinfo=ZoneInfo('Europe/Dublin')))
        self.assertEqual(len(self.service.jobs()),1)
        tasks=self.service.tasks(kind='schedule')['tasks']
        self.assertEqual(sorted(t['status'] for t in tasks),['completed','failed'])
        failed=next(t for t in tasks if t['status']=='failed')
        self.assertTrue(any('active job' in e['message'] for e in self.service.task_detail(failed['id'])['events']))

    def test_delete_does_not_recreate_or_delete_history_or_jobs(self):
        schedule=self.service.save_schedule(self.schedule_data())
        result=self.service.run_schedule(schedule)
        self.service.delete_schedule(schedule['id']);self.service.delete_schedule('default')
        fresh=Service(self.roots,self.base/'state',False,start_worker=False)
        self.assertEqual(fresh.schedules(),[])
        self.assertEqual(fresh.task_detail(result['task_id'])['task']['status'],'completed')
        self.assertEqual(len(fresh.jobs()),1)

    def test_toggle_one_schedule_leaves_another_enabled(self):
        one=self.service.save_schedule(self.schedule_data('One',enabled=True))
        two=self.service.save_schedule(self.schedule_data('Two',enabled=True))
        self.service.toggle_schedule(one['id'],False)
        self.assertFalse(self.service.schedule(one['id'])['enabled'])
        self.assertTrue(self.service.schedule(two['id'])['enabled'])
        self.assertIsNone(self.service.next_run(self.service.schedule(one['id']),one['id']))

    def test_schedule_margin_does_not_change_manual_or_other_schedule_margin(self):
        first=self.service.save_schedule(self.schedule_data('First',margin_gb=2))
        second=self.service.save_schedule(self.schedule_data('Second',margin_gb=3))
        self.service.save_schedule(self.schedule_data('Default',id='default',margin_gb=7))
        self.assertEqual(self.service.engine.margin,0)
        self.assertEqual(self.service.global_settings()['margin_gb'],0)
        self.assertEqual(self.service.schedule(first['id'])['margin_gb'],2)
        self.assertEqual(self.service.schedule(second['id'])['margin_gb'],3)

    def test_schedule_margin_snapshotted_into_job(self):
        schedule=self.service.save_schedule(self.schedule_data())
        result=self.service.run_schedule(schedule)
        self.assertEqual(self.service.jobs()[0]['request']['margin_gb'],0)
        self.service.save_global_settings({**self.service.global_settings(),'margin_gb':100000})
        self.service.process_one()
        self.assertEqual(self.service.jobs()[0]['status'],'completed')
        self.assertEqual(len(self.service.task_detail(result['task_id'])['children']),1)

    def test_schedule_validation_and_missing_id(self):
        for changes in ({'name':''},{'name':'x'*81},{'source':''},{'target':'ssd1'},{'timezone':'Invalid/Zone'}):
            with self.subTest(changes=changes):
                with self.assertRaises(MoveError):self.service.save_schedule(self.schedule_data(**changes))
        with self.assertRaises(MoveError):self.service.toggle_schedule('missing',True)
        with self.assertRaises(MoveError):self.service.delete_schedule('missing')

    def test_schedule_clock_rollbacks_once_per_rule(self):
        one=self.service.save_schedule(self.schedule_data('One',time='01:30',enabled=True))
        self.service.scheduler_tick(dt.datetime(2026,10,25,1,30,tzinfo=ZoneInfo('Europe/Dublin'),fold=0))
        self.service.scheduler_tick(dt.datetime(2026,10,25,1,30,tzinfo=ZoneInfo('Europe/Dublin'),fold=1))
        self.assertEqual(self.service.tasks(kind='schedule')['total'],1)
        self.assertEqual(len(self.service.jobs()),1)

    def test_skipped_locks_and_scan_errors_in_run_log(self):
        schedule=self.service.save_schedule(self.schedule_data())
        (self.folder/'.mvlock').touch()
        result=self.service.run_schedule(schedule)
        self.assertTrue(any('skipped: locked' in e['message'] for e in self.service.task_detail(result['task_id'])['events']))
        (self.folder/'.mvlock').unlink();(self.folder/'unsafe').symlink_to('episode')
        result=self.service.run_schedule(schedule)
        self.assertTrue(result['errors'])
        self.assertEqual(self.service.task_detail(result['task_id'])['task']['status'],'failed')
