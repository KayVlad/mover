import errno
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from app.engine import Engine, MoveError, snapshot

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.roots = {d:self.base/d for d in ('ssd1','drive1','drive2')}
        for root in self.roots.values():
            root.mkdir()
        self.engine = Engine(self.roots, require_mounts=False, margin=0)
        self.src = self.engine.folder('ssd1','Anime','Frieren')
        self.dst = self.engine.folder('drive1','Anime','Frieren')
        self.src.mkdir(parents=True)
        self.req = dict(source='ssd1',category='Anime',name='Frieren',target='drive1',destination_category='Anime')

    def tearDown(self):
        self.temp.cleanup()

    def put(self, path, content=b'original'):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(content)
        return path

    def move(self, **kwargs):
        return self.engine.move(self.req, **kwargs)

    def test_basic_move_hash_and_name(self):
        self.put(self.src/'episode', b'x'*2000000)
        self.move()
        self.assertFalse(self.src.exists())
        self.assertEqual((self.dst/'episode').read_bytes(),b'x'*2000000)

    def test_split_seasons_and_empty_source_season(self):
        (self.src/'Season1').mkdir()
        self.put(self.src/'Season2'/'e2')
        self.put(self.dst/'Season1'/'e1',b'archive')
        self.move()
        self.assertEqual((self.dst/'Season1'/'e1').read_bytes(),b'archive')
        self.assertEqual((self.dst/'Season2'/'e2').read_bytes(),b'original')
        self.assertFalse(self.src.exists())

    def test_empty_destination_season(self):
        self.put(self.src/'Season1'/'e1')
        (self.dst/'Season1').mkdir(parents=True)
        self.move()
        self.assertTrue((self.dst/'Season1'/'e1').exists())

    def test_empty_folder(self):
        (self.src/'empty').mkdir()
        self.move()
        self.assertTrue((self.dst/'empty').is_dir())
        self.assertFalse(self.src.exists())

    def test_category_change(self):
        self.put(self.src/'e1')
        self.req['destination_category']='Series'
        self.move()
        self.assertTrue((self.roots['drive1']/'Series/Frieren/e1').is_file())

    def test_same_drive_category_change(self):
        self.put(self.src/'e1')
        self.req.update(target='ssd1',destination_category='Movies')
        self.move()
        self.assertTrue((self.roots['ssd1']/'Movies/Frieren/e1').is_file())

    def test_every_drive_pair(self):
        for source in self.roots:
            for target in self.roots:
                if source==target:
                    continue
                with self.subTest(source=source,target=target):
                    name=source+'-'+target
                    folder=self.engine.folder(source,'Movies',name)
                    self.put(folder/'movie')
                    self.engine.move(dict(source=source,category='Movies',name=name,target=target))
                    self.assertFalse(folder.exists())
                    self.assertTrue((self.engine.folder(target,'Movies',name)/'movie').exists())

    def test_existing_identical_file_is_error(self):
        self.put(self.src/'e1')
        self.put(self.dst/'e1')
        with self.assertRaisesRegex(MoveError,'Existing destination'):
            self.move()
        self.assertTrue((self.src/'e1').exists())

    def test_different_collision_preflights_entire_tree(self):
        self.put(self.src/'safe')
        self.put(self.src/'conflict')
        self.put(self.dst/'conflict',b'different')
        with self.assertRaises(MoveError):
            self.move()
        self.assertFalse((self.dst/'safe').exists())
        self.assertEqual((self.dst/'conflict').read_bytes(),b'different')

    def test_file_directory_collisions_both_directions(self):
        for reverse in (False,True):
            with self.subTest(reverse=reverse):
                shutil.rmtree(self.src);self.src.mkdir()
                if self.dst.exists():shutil.rmtree(self.dst)
                if reverse:
                    self.put(self.src/'Season1')
                    (self.dst/'Season1').mkdir(parents=True)
                else:
                    (self.src/'Season1').mkdir()
                    self.put(self.dst/'Season1')
                with self.assertRaises(MoveError):self.move()

    def test_source_lock_and_parent_lock(self):
        for where in (self.src,self.src.parent):
            lock=self.put(where/'.mvlock',b'')
            with self.assertRaisesRegex(MoveError,'locked'):self.move()
            lock.unlink()

    def test_manual_override_copies_lock_without_unpinning_on_failure(self):
        self.put(self.src/'.mvlock',b'')
        self.put(self.src/'e1')
        self.req['override_lock']=True
        with patch('app.engine.tempfile.mkstemp',side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):self.move()
        self.assertTrue((self.src/'.mvlock').exists())

    def test_manual_override_success_preserves_destination_lock(self):
        self.put(self.src/'.mvlock',b'')
        self.req['override_lock']=True
        self.move()
        self.assertTrue((self.dst/'.mvlock').exists())

    def test_traversal_unknown_drive_and_category(self):
        for key,val in [('name','../escape'),('name','/tmp/escape'),('name','.'),('name',''),('name','a\\b'),('name','a\x00b'),('category','../../escape'),('destination_category','../escape'),('source','unknown'),('target','unknown')]:
            with self.subTest(key=key,value=val):
                req={**self.req,key:val}
                with self.assertRaises(MoveError):self.engine.plan(req)

    def test_same_destination(self):
        self.req['target']='ssd1'
        with self.assertRaisesRegex(MoveError,'same'):self.move()

    def test_source_symlink(self):
        self.put(self.src/'real')
        (self.src/'link').symlink_to('real')
        with self.assertRaisesRegex(MoveError,'Symlink'):self.move()

    def test_destination_symlink(self):
        self.dst.mkdir(parents=True)
        (self.dst/'link').symlink_to(self.base)
        with self.assertRaisesRegex(MoveError,'Symlink'):self.move()

    def test_destination_ancestor_symlink(self):
        (self.roots['drive1']/'Anime').symlink_to(self.base)
        with self.assertRaisesRegex(MoveError,'Symlink'):self.move()

    def test_fifo_refused(self):
        os.mkfifo(self.src/'pipe')
        with self.assertRaisesRegex(MoveError,'Special'):self.move()

    def test_hardlinked_file_refused(self):
        self.put(self.src/'one')
        os.link(self.src/'one',self.src/'two')
        with self.assertRaisesRegex(MoveError,'Hard-linked'):self.move()

    def test_scan_permission_error_is_not_empty_success(self):
        self.put(self.src/'e1')
        with patch('app.engine.os.scandir',side_effect=PermissionError('scan denied')):
            with self.assertRaises(PermissionError):self.move()
        self.assertTrue((self.src/'e1').exists())

    def test_disk_full_before_move(self):
        self.put(self.src/'e1')
        with patch('app.engine.shutil.disk_usage',return_value=shutil._ntuple_diskusage(10,10,0)):
            with self.assertRaisesRegex(MoveError,'Insufficient'):self.move()
        self.assertTrue((self.src/'e1').exists())

    def test_disk_full_during_copy(self):
        self.put(self.src/'e1')
        with patch('app.engine.os.fsync',side_effect=OSError(errno.ENOSPC,'full')):
            with self.assertRaises(OSError):self.move()
        self.assertTrue((self.src/'e1').exists())
        self.assertFalse((self.dst/'e1').exists())
        self.assertEqual(list(self.dst.glob('.mover-*')),[])

    def test_atomic_publish_race_never_overwrites(self):
        self.put(self.src/'e1')
        original=os.link
        def collide(src,dst):
            Path(dst).write_bytes(b'other writer')
            original(src,dst)
        with patch('app.engine.os.link',side_effect=collide):
            with self.assertRaises(FileExistsError):self.move()
        self.assertEqual((self.dst/'e1').read_bytes(),b'other writer')
        self.assertTrue((self.src/'e1').exists())

    def test_source_changed_during_copy_same_size(self):
        self.put(self.src/'e1',b'aaaa')
        done=[False]
        def progress(**kw):
            if kw['phase']=='copying' and kw.get('completed_bytes',0)>0 and not done[0]:
                (self.src/'e1').write_bytes(b'bbbb');done[0]=True
        with self.assertRaisesRegex(MoveError,'verification'):self.move(progress=progress)
        self.assertEqual((self.src/'e1').read_bytes(),b'bbbb')

    def test_new_file_during_verification_is_kept(self):
        self.put(self.src/'e1')
        def progress(**kw):
            if kw['phase']=='verifying':self.put(self.src/'new')
        with self.assertRaisesRegex(MoveError,'tree changed'):self.move(progress=progress)
        self.assertTrue((self.src/'new').exists())
        self.assertTrue((self.src/'e1').exists())

    def test_destination_corruption_before_verification(self):
        self.put(self.src/'e1')
        def progress(**kw):
            if kw['phase']=='verifying':(self.dst/'e1').write_bytes(b'xxxxxxxx')
        with self.assertRaisesRegex(MoveError,'verification'):self.move(progress=progress)
        self.assertTrue((self.src/'e1').exists())

    def test_source_change_before_cleanup(self):
        self.put(self.src/'e1')
        def progress(**kw):
            if kw['phase']=='cleaning':(self.src/'e1').write_bytes(b'changed!')
        with self.assertRaisesRegex(MoveError,'changed before cleanup'):self.move(progress=progress)
        self.assertEqual((self.src/'e1').read_bytes(),b'changed!')

    def test_new_file_before_cleanup_not_recursively_deleted(self):
        self.put(self.src/'e1')
        def progress(**kw):
            if kw['phase']=='cleaning':self.put(self.src/'new')
        with self.assertRaisesRegex(MoveError,'cleanup incomplete'):self.move(progress=progress)
        self.assertTrue((self.src/'new').exists())
        self.assertTrue((self.dst/'e1').exists())

    def test_unlink_error_is_reported(self):
        self.put(self.src/'e1')
        original=Path.unlink
        def fail(path,*args,**kwargs):
            if path==self.src/'e1':raise PermissionError('cannot delete')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail):
            with self.assertRaises(PermissionError):self.move()
        self.assertTrue((self.src/'e1').exists())
        self.assertTrue((self.dst/'e1').exists())

    def test_cancel_before_copy(self):
        self.put(self.src/'e1')
        with self.assertRaisesRegex(MoveError,'Cancelled'):self.move(cancelled=lambda:True)
        self.assertFalse(self.dst.exists())

    def test_cancel_mid_copy(self):
        self.put(self.src/'e1',b'a'*3000000)
        stop=[False]
        def progress(**kw):
            if kw.get('completed_bytes',0)>0:stop[0]=True
        with self.assertRaisesRegex(MoveError,'Cancelled'):self.move(progress=progress,cancelled=lambda:stop[0])
        self.assertTrue((self.src/'e1').exists())
        self.assertFalse((self.dst/'e1').exists())

    def test_drive_disappears(self):
        shutil.rmtree(self.roots['drive2'])
        with self.assertRaisesRegex(MoveError,'unavailable'):self.move()

    def test_drive_identity_changes(self):
        self.roots['drive1'].rename(self.base/'old-drive')
        self.roots['drive1'].mkdir()
        with self.assertRaisesRegex(MoveError,'identity changed'):self.move()

    def test_mount_required(self):
        with self.assertRaisesRegex(MoveError,'not mounted'):Engine(self.roots,require_mounts=True)

    def test_overlapping_roots(self):
        with self.assertRaisesRegex(MoveError,'overlapping'):Engine({'ssd1':self.base,'drive1':self.roots['drive1']},False)

    def test_lock_appears_mid_copy(self):
        self.put(self.src/'e1',b'a'*2000000)
        def progress(**kw):
            if kw.get('completed_bytes',0)>0:self.put(self.src.parent/'.mvlock',b'')
        with self.assertRaisesRegex(MoveError,'locked during'):self.move(progress=progress)
        self.assertTrue((self.src/'e1').exists())

    def test_review_changed_source(self):
        self.put(self.src/'e1')
        plan=self.engine.plan(self.req)
        request={**self.req,'reviewed_snapshot':json.loads(json.dumps(plan['_snapshot']))}
        self.put(self.src/'new')
        with self.assertRaisesRegex(MoveError,'since review'):self.engine.move(request)

    def test_unicode_spaces_newlines(self):
        self.req['name']='葬送のフリーレン with spaces\nline'
        self.src=self.engine.folder('ssd1','Anime',self.req['name'])
        self.put(self.src/'episode\n1')
        self.move()
        self.assertTrue((self.engine.folder('drive1','Anime',self.req['name'])/'episode\n1').exists())

    def test_destination_must_be_explicit(self):
        self.put(self.src/'e1')
        for value in (None, ''):
            with self.subTest(value=value):
                self.req['target']=value
                with self.assertRaisesRegex(MoveError,'Choose a destination'):self.move()
        request={k:v for k,v in self.req.items() if k!='target'}
        with self.assertRaisesRegex(MoveError,'Choose a destination'):self.engine.plan(request)

    def test_auto_uses_only_explicit_cold_roles_and_most_free(self):
        self.put(self.src/'e1')
        self.req['target']='auto'
        self.engine.location_roles={'ssd1':'cache','drive1':'cold','drive2':'cold'}
        free={'ssd1':10000,'drive1':100,'drive2':1000}
        with patch('app.engine.shutil.disk_usage',side_effect=lambda p:shutil._ntuple_diskusage(10000,0,free[Path(p).name])):
            self.assertEqual(self.engine.plan(self.req)['target'],'drive2')
        self.engine.location_roles['drive2']='cache'
        with patch('app.engine.shutil.disk_usage',side_effect=lambda p:shutil._ntuple_diskusage(10000,0,free[Path(p).name])):
            self.assertEqual(self.engine.plan(self.req)['target'],'drive1')

    def test_auto_never_infers_roles_from_drive_names(self):
        self.put(self.src/'e1');self.req['target']='auto'
        with self.assertRaisesRegex(MoveError,'assigned as cold'):self.move()
        self.engine.location_roles={'ssd1':'cold','drive1':'cache','drive2':'unassigned'}
        with self.assertRaisesRegex(MoveError,'assigned as cold'):self.move()

    def test_ssd_named_location_can_be_cold_destination(self):
        self.put(self.dst/'e1')
        self.engine.location_roles={'ssd1':'cold','drive1':'cache','drive2':'unassigned'}
        plan=self.engine.plan({**self.req,'source':'drive1','target':'auto'})
        self.assertEqual(plan['target'],'ssd1')

    def test_auto_cold_no_space(self):
        self.put(self.src/'e1');self.req['target']='auto'
        self.engine.location_roles={'drive1':'cold'}
        with patch('app.engine.shutil.disk_usage',return_value=shutil._ntuple_diskusage(0,0,0)):
            with self.assertRaisesRegex(MoveError,'No cold storage.*enough free'):self.move()

    def test_arbitrary_location_names_have_no_roles(self):
        engine=Engine({'Incoming files':self.roots['drive2'],'Library':self.roots['ssd1']},False,0)
        folder=engine.folder('Incoming files','Movies','Generic')
        self.put(folder/'movie')
        engine.move(dict(source='Incoming files',target='Library',category='Movies',name='Generic'))
        self.assertFalse(folder.exists())
        self.assertTrue((engine.folder('Library','Movies','Generic')/'movie').exists())

    def test_third_branch_collision_blocks_hidden_mergerfs_duplicates(self):
        self.put(self.src/'Season1'/'e1')
        third=self.engine.folder('drive2','Anime','Frieren')
        self.put(third/'Season1'/'e1',b'old hidden file')
        with self.assertRaisesRegex(MoveError,'drive2/Season1/e1'):
            self.move()
        self.assertFalse(self.dst.exists())
        self.assertTrue((self.src/'Season1'/'e1').exists())

    def test_category_review_exposes_other_source_branches(self):
        self.put(self.src/'Season2'/'e2')
        self.put(self.dst/'Season1'/'e1')
        self.req['destination_category']='Series'
        plan=self.engine.plan(self.req)
        self.assertEqual(plan['source_branches'],[dict(drive='drive1',category='Anime',files=1)])

    def test_read_failure_keeps_source(self):
        self.put(self.src/'e1')
        with patch('app.engine.digest',side_effect=OSError('read error')):
            with self.assertRaises(OSError):self.move()
        self.assertTrue((self.src/'e1').exists())

    def test_destination_creation_permission_error(self):
        self.put(self.src/'e1')
        original=Path.mkdir
        def refuse(path,*args,**kwargs):
            if path==self.dst:raise PermissionError('mkdir denied')
            return original(path,*args,**kwargs)
        with patch.object(Path,'mkdir',refuse):
            with self.assertRaises(PermissionError):self.move()
        self.assertTrue((self.src/'e1').exists())

    def test_partial_copy_is_never_automatically_overwritten_on_retry(self):
        self.put(self.src/'e1')
        def progress(**kw):
            if kw['phase']=='verifying':self.put(self.src/'new')
        with self.assertRaises(MoveError):self.move(progress=progress)
        with self.assertRaisesRegex(MoveError,'Existing destination'):self.move()
        self.assertTrue((self.src/'e1').exists())

    def test_generated_split_layouts_preserve_union(self):
        import random
        for seed in range(30):
            rng=random.Random(seed)
            for source_drive in self.roots:
                for target_drive in self.roots:
                    if source_drive==target_drive:continue
                    with self.subTest(seed=seed,source=source_drive,target=target_drive):
                        name=f'Layout-{seed}-{source_drive}-{target_drive}'
                        expected={}
                        for drive in self.roots:
                            root=self.engine.folder(drive,'Anime',name)
                            root.mkdir(parents=True)
                            for season in range(3):
                                if rng.randrange(2):(root/f'Season{season}').mkdir()
                        for i in range(8):
                            drive=source_drive if i==0 else rng.choice(list(self.roots))
                            rel=f'Season{i%3}/episode-{i}'
                            content=f'{seed}:{drive}:{i}'.encode()
                            self.put(self.engine.folder(drive,'Anime',name)/rel,content)
                            expected[rel]=content
                        self.engine.move(dict(source=source_drive,category='Anime',name=name,target=target_drive))
                        actual={}
                        for drive in self.roots:
                            root=self.engine.folder(drive,'Anime',name)
                            if root.exists():
                                for p in root.rglob('*'):
                                    if p.is_file():
                                        rel=str(p.relative_to(root))
                                        self.assertNotIn(rel,actual)
                                        actual[rel]=p.read_bytes()
                        self.assertEqual(actual,expected)
                        self.assertFalse(self.engine.folder(source_drive,'Anime',name).exists())

    def test_real_source_read_permission_failure(self):
        f=self.put(self.src/'e1')
        f.chmod(0)
        try:
            with self.assertRaises(PermissionError):self.move()
            self.assertTrue(f.exists())
        finally:f.chmod(0o600)

    def test_real_destination_permission_failure(self):
        self.put(self.src/'e1')
        self.dst.mkdir(parents=True)
        self.dst.chmod(0o555)
        try:
            with self.assertRaises(PermissionError):self.move()
            self.assertTrue((self.src/'e1').exists())
        finally:self.dst.chmod(0o755)

    def test_real_cleanup_permission_failure(self):
        self.put(self.src/'e1')
        self.src.chmod(0o555)
        try:
            with self.assertRaises(PermissionError):self.move()
            self.assertTrue((self.src/'e1').exists())
            self.assertTrue((self.dst/'e1').exists())
        finally:self.src.chmod(0o755)

    def test_drive_lock_blocks_independent_instance(self):
        import fcntl
        self.put(self.src/'e1')
        media=self.src.parent.parent
        with open(media/'.mover-app.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(MoveError,'drive lock'):self.move()
        self.assertTrue((self.src/'e1').exists())

    def test_sigkill_after_copy_keeps_both_copies(self):
        import subprocess,sys,time
        self.put(self.src/'e1',b'a'*2000000)
        marker=self.base/'copied-marker'
        code="""
import json,sys,time
from pathlib import Path
from app.engine import Engine
roots,request,marker=json.loads(sys.argv[1]),json.loads(sys.argv[2]),Path(sys.argv[3])
def progress(**kw):
    if kw['phase']=='verifying':
        marker.write_text('copied')
        time.sleep(30)
Engine(roots,False,0).move(request,progress=progress)
"""
        proc=subprocess.Popen([sys.executable,'-c',code,json.dumps({k:str(v) for k,v in self.roots.items()}),json.dumps(self.req),str(marker)])
        try:
            deadline=time.monotonic()+10
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(marker.exists())
            proc.kill();proc.wait(timeout=5)
            self.assertEqual((self.src/'e1').read_bytes(),b'a'*2000000)
            self.assertEqual((self.dst/'e1').read_bytes(),b'a'*2000000)
            with self.assertRaisesRegex(MoveError,'Existing destination'):self.move()
        finally:
            if proc.poll() is None:proc.kill();proc.wait()

if __name__=='__main__':unittest.main()
