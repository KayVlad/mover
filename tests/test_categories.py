import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.engine import Engine, MoveError
from app.server import Service, configured_roots

class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.roots = {f'Location {i}': self.base / f'media-{i}' for i in range(6)}
        for root in self.roots.values():
            root.mkdir()
        self.engine = Engine(self.roots, require_mounts=False, margin=0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discovery_and_missing_destination_creation(self):
        source = self.roots['Location 0'] / 'Documentaries & Music' / 'Example'
        source.mkdir(parents=True)
        (source / 'file').write_bytes(b'content')
        (self.roots['Location 5'] / 'Books').mkdir()
        (self.roots['Location 0'] / '.internal').mkdir()
        (self.roots['Location 0'] / 'loose-file').write_text('ignored')
        (self.roots['Location 0'] / 'linked').symlink_to(source, target_is_directory=True)
        self.assertEqual(self.engine.categories(), ['Books', 'Documentaries & Music'])
        request = dict(source='Location 0', target='Location 5', category='Documentaries & Music', name='Example')
        plan = self.engine.plan(request)
        self.assertTrue(plan['creates_category'])
        self.assertFalse((self.roots['Location 5'] / request['category']).exists())
        self.engine.move(request)
        self.assertEqual((self.roots['Location 5'] / request['category'] / 'Example/file').read_bytes(), b'content')
        self.assertFalse(source.exists())

    def test_category_changes_are_discovered_without_restart(self):
        service = Service(self.roots, self.base / 'state', False, start_worker=False)
        self.assertEqual(service.state()['categories'], [])
        category = self.roots['Location 3'] / 'New category'
        category.mkdir()
        self.assertEqual(service.state(refresh=True)['categories'], ['New category'])
        category.rmdir()
        self.assertEqual(service.state(refresh=True)['categories'], [])

    def test_invalid_or_symlink_destination_categories_refused(self):
        for name in ['', '.', '..', '../outside', 'a/b', 'a\\b', '.internal', 'a\x00b']:
            with self.subTest(name=name), self.assertRaises(MoveError):
                self.engine.folder('Location 0', name)
        (self.roots['Location 1'] / 'Unsafe').symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(MoveError):
            self.engine.folder('Location 1', 'Unsafe')

    def test_single_location_configuration(self):
        with patch.dict('os.environ', {'MOVER_ROOTS': '{"Only": "/media/only"}'}):
            self.assertEqual(configured_roots(), {'Only': '/media/only'})

    def test_category_and_folder_filesystem_boundaries_refused(self):
        import os
        original = Path.stat
        for drive in ['Location 0', 'Location 5']:
            for depth in ['category', 'folder']:
                with self.subTest(drive=drive, depth=depth):
                    category = self.roots[drive] / 'Foreign filesystem'
                    folder = category / 'Example'
                    folder.mkdir(parents=True, exist_ok=True)
                    foreign = category if depth == 'category' else folder
                    def foreign_stat(path, *args, **kwargs):
                        result = original(path, *args, **kwargs)
                        if path == foreign:
                            values = list(result)
                            values[2] += 1
                            return os.stat_result(values)
                        return result
                    with patch.object(Path, 'stat', foreign_stat):
                        with self.assertRaisesRegex(MoveError, 'Nested filesystem'):
                            self.engine.folder(drive, category.name, folder.name)
