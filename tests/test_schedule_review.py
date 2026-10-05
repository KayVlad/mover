import json
from pathlib import Path
from unittest.mock import patch
from test_service import ServiceFixture
from app.engine import MoveError

class ScheduleReviewTests(ServiceFixture):
    def schedule(self, **changes):
        data={k:v for k,v in self.service.settings().items() if k!='location_roles'}
        return self.service.save_schedule(dict(data,name='Reviewed route',**changes))

    def add(self, name):
        folder=self.service.engine.folder('ssd1','Anime',name)
        folder.mkdir(parents=True)
        (folder/'episode').write_bytes(b'new')
        return folder

    def test_only_reviewed_folders_are_queued_and_executed(self):
        schedule=self.schedule(age_days=0)
        review=self.service.review_schedule(schedule['id'])
        new=self.add('Added after preview')
        result=self.service.confirm_schedule_review(schedule['id'],review['review_token'])
        self.assertEqual([p['name'] for p in result['folders']],['Frieren'])
        self.assertEqual(len(self.service.task_detail(result['task_id'])['children']),1)
        self.service.process_one()
        self.assertTrue(new.exists())
        self.assertFalse(self.folder.exists())

    def test_changed_folder_rejects_entire_confirmation(self):
        self.add('Second')
        schedule=self.schedule(age_days=0)
        review=self.service.review_schedule(schedule['id'])
        (self.folder/'episode').write_bytes(b'changed')
        with self.assertRaisesRegex(MoveError,'changed since'):
            self.service.confirm_schedule_review(schedule['id'],review['review_token'])
        self.assertEqual(self.service.jobs(),[])

    def test_changed_schedule_or_roles_requires_new_preview(self):
        for change in ['schedule','roles']:
            with self.subTest(change=change):
                schedule=self.schedule(age_days=0)
                review=self.service.review_schedule(schedule['id'])
                if change=='schedule':
                    self.service.save_schedule({**schedule,'target':'drive2'})
                else:
                    settings=self.service.global_settings()
                    self.service.save_global_settings({**settings,'location_roles':{**settings['location_roles'],'drive2':'cold'}})
                with self.assertRaisesRegex(MoveError,'changed since review'):
                    self.service.confirm_schedule_review(schedule['id'],review['review_token'])
                self.assertEqual(self.service.jobs(),[])

    def test_review_expiry_reuse_and_wrong_schedule_refused(self):
        first=self.schedule(age_days=0)
        second=self.schedule(age_days=0)
        review=self.service.review_schedule(first['id'])
        with self.assertRaises(MoveError):
            self.service.confirm_schedule_review(second['id'],review['review_token'])
        review=self.service.review_schedule(first['id'])
        record=self.service.schedule_reviews[review['review_token']]
        self.service.schedule_reviews[review['review_token']]=(0,*record[1:])
        with self.assertRaisesRegex(MoveError,'expired'):
            self.service.confirm_schedule_review(first['id'],review['review_token'])
        review=self.service.review_schedule(first['id'])
        self.service.confirm_schedule_review(first['id'],review['review_token'])
        with self.assertRaisesRegex(MoveError,'expired'):
            self.service.confirm_schedule_review(first['id'],review['review_token'])

    def test_duplicate_queue_rolls_back_all_reviewed_jobs(self):
        self.add('Second')
        schedule=self.schedule(age_days=0)
        review=self.service.review_schedule(schedule['id'])
        self.service.enqueue(dict(source='ssd1',target='drive1',category='Anime',name='Second'))
        with self.assertRaisesRegex(MoveError,'active job'):
            self.service.confirm_schedule_review(schedule['id'],review['review_token'])
        self.assertEqual([j['request']['name'] for j in self.service.jobs()],['Second'])
        self.assertEqual(self.service.tasks(kind='schedule')['total'],0)

    def test_space_loss_after_preview_rejects_all(self):
        import shutil
        schedule=self.schedule(age_days=0)
        review=self.service.review_schedule(schedule['id'])
        with patch('app.engine.shutil.disk_usage',return_value=shutil._ntuple_diskusage(1,1,0)):
            with self.assertRaises(MoveError):
                self.service.confirm_schedule_review(schedule['id'],review['review_token'])
        self.assertEqual(self.service.jobs(),[])

    def test_collisions_after_preview_reject_confirmation(self):
        schedule=self.schedule(age_days=0)
        review=self.service.review_schedule(schedule['id'])
        target=self.service.engine.folder('drive1','Anime','Frieren')
        target.mkdir(parents=True)
        (target/'episode').write_bytes(b'other')
        with self.assertRaisesRegex(MoveError,'collision'):
            self.service.confirm_schedule_review(schedule['id'],review['review_token'])
        self.assertEqual(self.service.jobs(),[])

    def test_automatic_destination_remains_the_reviewed_location(self):
        import shutil
        settings=self.service.global_settings()
        self.service.save_global_settings({**settings,'location_roles':{'ssd1':'cache','drive1':'cold','drive2':'cold'}})
        schedule=self.schedule(age_days=0,target='auto')
        free={'ssd1':1000,'drive1':2000,'drive2':1000}
        def usage(path):
            return shutil._ntuple_diskusage(10000,0,free[Path(path).name])
        with patch('app.engine.shutil.disk_usage',side_effect=usage):
            review=self.service.review_schedule(schedule['id'])
            self.assertEqual(review['folders'][0]['target'],'drive1')
            free['drive2']=5000
            result=self.service.confirm_schedule_review(schedule['id'],review['review_token'])
            self.assertEqual(result['folders'][0]['target'],'drive1')

    def test_lock_change_invalidates_schedule_review(self):
        schedule=self.schedule(age_days=0)
        review=self.service.review_schedule(schedule['id'])
        self.service.set_folder_lock(dict(drive='ssd1',category='Anime',name='Frieren',locked=True))
        with self.assertRaisesRegex(MoveError,'expired'):
            self.service.confirm_schedule_review(schedule['id'],review['review_token'])
        self.assertEqual(self.service.jobs(),[])

    def test_preview_errors_are_retained_when_valid_reviewed_folders_run(self):
        bad=self.add('Bad')
        (bad/'unsafe').symlink_to('episode')
        schedule=self.schedule(age_days=0)
        review=self.service.review_schedule(schedule['id'])
        self.assertTrue(review['errors'])
        result=self.service.confirm_schedule_review(schedule['id'],review['review_token'])
        self.assertEqual([p['name'] for p in result['folders']],['Frieren'])
        self.assertEqual(result['errors'],review['errors'])
        self.assertEqual(self.service.task_detail(result['task_id'])['task']['status'],'failed')
