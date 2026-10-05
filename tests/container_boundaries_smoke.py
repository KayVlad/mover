"""Verify real filesystem boundaries using isolated bind mounts and tmpfs."""
import os
from pathlib import Path
import subprocess
import tempfile

with tempfile.TemporaryDirectory(prefix='mover-boundary-') as tmp:
    base = Path(tmp)
    (base/'a/Nested').mkdir(parents=True)
    (base/'b/Category/Folder').mkdir(parents=True)
    code = """
from app.engine import Engine, MoveError
engine = Engine({'A':'/mediaA','B':'/mediaB'},True,0)
for drive,category,folder in [('A','Nested','Folder'),('B','Category','Folder')]:
    try:
        engine.folder(drive,category,folder)
    except MoveError as error:
        assert 'Nested filesystem' in str(error), str(error)
    else:
        raise AssertionError('Nested mount was accepted')
print('PASS: real tmpfs category and folder mounts refused under bind-mounted media roots')
"""
    subprocess.run(['podman','run','--rm','--userns=keep-id',
        '--user',f'{os.getuid()}:{os.getgid()}', '--read-only','--cap-drop=ALL',
        '--security-opt=no-new-privileges', '-v',f'{base/"a"}:/mediaA',
        '-v',f'{base/"b"}:/mediaB', '--tmpfs','/mediaA/Nested:rw',
        '--tmpfs','/mediaB/Category/Folder:rw',
        os.environ.get('MOVER_TEST_IMAGE','localhost/mover:latest'), 'python','-c',code], check=True)
