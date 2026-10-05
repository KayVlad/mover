"""Destructive tests confined to fresh temporary media roots."""
import os
from pathlib import Path
import tempfile
import unittest
from app.engine import Engine, MoveError

class AdversarialTests(unittest.TestCase):
    def setup_move(self, base):
        roots = {f'Drive {i}': base / f'media{i}' for i in range(6)}
        for root in roots.values(): root.mkdir()
        engine = Engine(roots, False, 0)
        source = roots['Drive 0'] / 'Custom category' / 'Folder'
        source.mkdir(parents=True)
        expected = {'first': b'a' * 1100000, 'nested/second': b'b' * 1500000}
        for rel, content in expected.items():
            path = source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        destination = roots['Drive 5'] / 'Custom category' / 'Folder'
        request = dict(source='Drive 0', target='Drive 5', category='Custom category', name='Folder')
        return engine, roots, source, destination, request, expected

    def assert_content_survives(self, source, destination, expected):
        for rel, content in expected.items():
            copies = [p.read_bytes() for p in (source / rel, destination / rel) if p.is_file()]
            self.assertIn(content, copies, rel)

    def test_cancellation_at_every_reported_phase_preserves_content(self):
        phases = ['copying', 'file_copied', 'verifying', 'file_verified', 'cleaning', 'source_removed', 'directory_removed']
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                engine, roots, src, dst, req, expected = self.setup_move(Path(tmp))
                stop = [False]
                def progress(**event):
                    if event['phase'] == phase: stop[0] = True
                with self.assertRaises(MoveError):
                    engine.move(req, progress=progress, cancelled=lambda: stop[0])
                self.assert_content_survives(src, dst, expected)

    def test_drive_replacement_during_copy_and_cleanup_preserves_content(self):
        for phase in ['copying', 'file_copied', 'verifying', 'cleaning', 'source_removed']:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                engine, roots, src, dst, req, expected = self.setup_move(base)
                fired = [False]
                old_root = base / 'disconnected-drive'
                def progress(**event):
                    if event['phase'] == phase and not fired[0]:
                        fired[0] = True
                        roots['Drive 5'].rename(old_root)
                        roots['Drive 5'].mkdir()
                with self.assertRaises(MoveError): engine.move(req, progress=progress)
                self.assert_content_survives(src, old_root / 'Custom category/Folder', expected)

    def test_destination_corruption_at_copy_verify_and_cleanup_is_detected(self):
        for phase in ['file_copied', 'verifying', 'file_verified', 'cleaning']:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                engine, roots, src, dst, req, expected = self.setup_move(Path(tmp))
                fired = [False]
                def progress(**event):
                    if event['phase'] == phase and not fired[0]:
                        fired[0] = True
                        (dst / 'first').write_bytes(b'corrupted')
                with self.assertRaises(MoveError): engine.move(req, progress=progress)
                self.assert_content_survives(src, dst, expected)
                self.assertEqual((src / 'first').read_bytes(), expected['first'])

    def test_destination_symlink_swap_does_not_touch_outside_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            engine, roots, src, dst, req, expected = self.setup_move(base)
            outside = base / 'outside'
            outside.mkdir()
            sentinel = outside / 'first'
            sentinel.write_bytes(b'untouchable')
            fired = [False]
            def progress(**event):
                if event['phase'] == 'copying' and not fired[0]:
                    fired[0] = True
                    dst.rename(dst.with_name('original'))
                    dst.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(MoveError): engine.move(req, progress=progress)
            self.assertEqual(sentinel.read_bytes(), b'untouchable')
            for rel, content in expected.items(): self.assertEqual((src / rel).read_bytes(), content)

    def test_third_branch_collision_appearing_during_copy_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, roots, src, dst, req, expected = self.setup_move(Path(tmp))
            fired = [False]
            def progress(**event):
                if event['phase'] == 'file_copied' and not fired[0]:
                    fired[0] = True
                    collision = roots['Drive 3'] / 'Custom category/Folder/first'
                    collision.parent.mkdir(parents=True)
                    collision.write_bytes(b'other branch writer')
            with self.assertRaisesRegex(MoveError, 'collision'):
                engine.move(req, progress=progress)
            for rel, content in expected.items(): self.assertEqual((src / rel).read_bytes(), content)

    def test_sigkill_mid_copy_and_mid_cleanup_preserves_content(self):
        import json
        import subprocess
        import sys
        import time
        code = """
import json,sys,time
from pathlib import Path
from app.engine import Engine
roots,request,marker,phase=json.loads(sys.argv[1]),json.loads(sys.argv[2]),Path(sys.argv[3]),sys.argv[4]
def progress(**event):
    if event['phase']==phase and (phase!='copying' or event['completed_bytes']>0):
        marker.write_text('ready')
        time.sleep(30)
Engine(roots,False,0).move(request,progress=progress)
"""
        for phase in ['copying', 'source_removed']:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                engine, roots, src, dst, req, expected = self.setup_move(base)
                marker = base / 'marker'
                proc = subprocess.Popen([sys.executable, '-c', code,
                    json.dumps({k: str(v) for k, v in roots.items()}), json.dumps(req), str(marker), phase])
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(marker.exists(), 'child did not reach crash point')
                    proc.kill()
                    proc.wait(timeout=5)
                    self.assert_content_survives(src, dst, expected)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)

    def test_open_writer_can_continue_writing_an_unlinked_source(self):
        # Documented limitation: open descriptors aren't detected or redirected.
        with tempfile.TemporaryDirectory() as tmp:
            engine, roots, src, dst, req, expected = self.setup_move(Path(tmp))
            with (src / 'first').open('r+b') as writer:
                engine.move(req)
                writer.seek(0)
                writer.write(b'late writer update')
                writer.flush()
                self.assertEqual(os.fstat(writer.fileno()).st_nlink, 0)
                self.assertEqual((dst / 'first').read_bytes(), expected['first'])
                self.assertFalse(src.exists())
