"""Conservative branch-to-branch moves. Never operate through mergerfs."""
import hashlib
import fcntl
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

def valid_category(name):
    return (isinstance(name, str) and bool(name) and not name.startswith('.')
            and '/' not in name and '\\' not in name and '\x00' not in name)


class MoveError(Exception):
    pass

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def signature(path):
    s = path.lstat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)

def safe_path(root, category, name=None):
    if not valid_category(category):
        raise MoveError('Invalid category name')
    if name is not None and (not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\\' in name or '\x00' in name):
        raise MoveError('Invalid folder name')
    root = Path(root)
    result = root / category
    if name is not None:
        result /= name
    cur = root
    if cur.is_symlink() or not cur.is_dir():
        raise MoveError('Drive root is missing or a symlink')
    device = root.stat().st_dev
    for part in result.relative_to(root).parts:
        cur /= part
        if cur.is_symlink():
            raise MoveError(f'Symlink path refused: {cur}')
        if cur.exists() and not cur.is_dir():
            raise MoveError(f'Expected directory: {cur}')
        if cur.exists() and cur.stat().st_dev != device:
            raise MoveError(f'Nested filesystem refused: {cur}')
    return result

def snapshot(folder):
    """Fail closed on scan errors, symlinks, special files, nested filesystems."""
    files, dirs = {}, {}
    device = folder.stat().st_dev
    def scan(path):
        dirs[str(path.relative_to(folder))] = signature(path)
        with os.scandir(path) as entries:
            for entry in entries:
                p = Path(entry.path)
                s = p.lstat()
                if s.st_dev != device:
                    raise MoveError('Nested filesystem refused')
                if stat.S_ISLNK(s.st_mode):
                    raise MoveError(f'Symlink refused: {p}')
                if stat.S_ISDIR(s.st_mode):
                    scan(p)
                elif stat.S_ISREG(s.st_mode):
                    if s.st_nlink != 1:
                        raise MoveError(f'Hard-linked source file refused: {p}')
                    files[str(p.relative_to(folder))] = signature(p)
                else:
                    raise MoveError(f'Special file refused: {p}')
    if folder.is_symlink() or not folder.is_dir():
        raise MoveError('Source folder missing or unsafe')
    scan(folder)
    return files, dirs

def locked(folder, category_root):
    return os.path.lexists(folder / '.mvlock') or os.path.lexists(category_root / '.mvlock')

class Engine:
    def __init__(self, roots, require_mounts=True, margin=5 * 1024**3, location_roles=None):
        self.roots = {k: Path(v).absolute() for k, v in roots.items()}
        self.require_mounts = require_mounts
        self.location_roles = dict(location_roles or {})
        self.margin = margin
        self.identities = {}
        self.check_drives(initial=True)

    def check_drives(self, initial=False):
        resolved = []
        for name, root in self.roots.items():
            if root.resolve() != root or root.is_symlink() or not root.is_dir():
                raise MoveError(f'{name}: drive unavailable')
            if self.require_mounts and not os.path.ismount(root):
                raise MoveError(f'{name}: drive is not mounted')
            ident = (root.stat().st_dev, root.stat().st_ino)
            if not initial and self.identities[name] != ident:
                raise MoveError(f'{name}: drive identity changed; restart after checking mounts')
            self.identities[name] = ident
            resolved.append(root.resolve())
        for i, root in enumerate(resolved):
            for other in resolved[i+1:]:
                if root == other or root in other.parents or other in root.parents:
                    raise MoveError('Drive roots must be separate, non-overlapping directories')

    def folder(self, drive, category, name=None):
        if drive not in self.roots:
            raise MoveError('Unknown drive')
        return safe_path(self.roots[drive], category, name)

    def categories(self):
        self.check_drives()
        names = set()
        for root in self.roots.values():
            with os.scandir(root) as entries:
                for entry in entries:
                    if valid_category(entry.name) and entry.is_dir(follow_symlinks=False):
                        names.add(entry.name)
        return sorted(names)

    def inventory(self):
        import time
        self.check_drives()
        rows = []
        for drive in self.roots:
            for cat in self.categories():
                base = self.folder(drive, cat)
                if not base.exists():
                    continue
                for child in sorted(base.iterdir()):
                    if not child.is_dir() and not child.is_symlink():
                        continue
                    row = dict(drive=drive, category=cat, name=child.name, locked=locked(child, base),
                               folder_locked=os.path.lexists(child / '.mvlock'),
                               parent_locked=os.path.lexists(base / '.mvlock'))
                    try:
                        files, dirs = snapshot(child)
                        row.update(size=sum(s[2] for s in files.values()), files=len(files),
                                   age=int((time.time_ns()-max(s[3] for s in files.values())) / (86400*10**9)) if files else None,
                                   error=None)
                    except (OSError, MoveError) as e:
                        row.update(size=0, files=0, age=None, error=str(e))
                    rows.append(row)
        return rows

    def plan(self, request):
        self.check_drives()
        source = self.folder(request['source'], request['category'], request['name'])
        source_base = self.folder(request['source'], request['category'])
        if locked(source, source_base) and not request.get('override_lock', False):
            raise MoveError('Folder is locked; explicit manual override required')
        files, dirs = snapshot(source)
        category = request.get('destination_category', request['category'])
        target = request.get('target')
        if not isinstance(target, str) or not target or target not in (*self.roots, 'auto'):
            raise MoveError('Choose a destination location or cold-storage policy')
        size = sum(s[2] for s in files.values())
        margin_gb=request.get('margin_gb', self.margin//1024**3)
        if type(margin_gb) is not int or not 0<=margin_gb<=100000:raise MoveError('Invalid free-space margin')
        margin=margin_gb*1024**3
        if target == 'auto':
            candidates = [name for name in self.roots if name != request['source'] and self.location_roles.get(name) == 'cold']
            if not candidates:
                raise MoveError('No other locations are assigned as cold storage')
            candidates.sort(key=lambda name: shutil.disk_usage(self.roots[name]).free, reverse=True)
            target = next((name for name in candidates if shutil.disk_usage(self.roots[name]).free >= size + margin), None)
            if target is None:
                raise MoveError('No cold storage location has enough free space including margin')
        dest = self.folder(target, category, request['name'])
        if source == dest:
            raise MoveError('Source and destination are the same')
        if shutil.disk_usage(self.roots[target]).free < size + margin:
            raise MoveError('Insufficient destination free space including margin')
        # Inspect whole destination: do not follow unsafe pre-existing paths.
        conflicts, other_branches = [], []
        # Mergerfs can hide duplicate file paths on a third branch. Check every
        # physical branch of the destination category, not just the target.
        for drive in self.roots:
            branch = self.folder(drive, category, request['name'])
            if branch == source or not branch.exists():
                continue
            branch_files, branch_dirs = snapshot(branch)
            other_branches.append(dict(drive=drive, category=category, files=len(branch_files)))
            for rel in dirs:
                if rel in branch_files:
                    conflicts.append(f'{drive}/{rel}')
            for rel in files:
                if rel in branch_files or rel in branch_dirs:
                    conflicts.append(f'{drive}/{rel}')
        source_branches = []
        if category != request['category']:
            for drive in self.roots:
                branch = self.folder(drive, request['category'], request['name'])
                if branch != source and branch.exists():
                    branch_files, _ = snapshot(branch)
                    source_branches.append(dict(drive=drive, category=request['category'], files=len(branch_files)))
        if conflicts:
            raise MoveError('Existing destination file or file/directory collision: ' + ', '.join(conflicts[:8]))
        # Review must not accept a source that changed during preflight.
        if snapshot(source) != (files, dirs):
            raise MoveError('Source changed during review')
        return dict(source=request['source'], category=request['category'], name=request['name'],
                    target=target, destination_category=category,margin_gb=margin_gb, override_lock=bool(request.get('override_lock', False)),
                    size=size, files=len(files), merge=dest.exists(), other_branches=other_branches, source_branches=source_branches,
                    destination=str(dest), creates_category=not dest.parent.exists(), _snapshot=(files, dirs))

    def check_other_branches(self, plan, files, dirs):
        """Catch collisions created on other physical branches during copying."""
        self.check_drives()
        source = self.folder(plan['source'], plan['category'], plan['name'])
        for drive in self.roots:
            if drive == plan['target']:
                continue
            branch = self.folder(drive, plan['destination_category'], plan['name'])
            if branch == source or not branch.exists():
                continue
            branch_files, branch_dirs = snapshot(branch)
            collisions = (set(files) & (set(branch_files) | set(branch_dirs))) | (set(dirs) & set(branch_files))
            if collisions:
                raise MoveError('Other branch collision appeared during move: ' + drive + '/' + sorted(collisions)[0])

    @contextmanager
    def branch_locks(self):
        self.check_drives()
        with ExitStack() as stack:
            for drive in sorted(self.roots):
                media = self.roots[drive]
                media.mkdir(parents=True, exist_ok=True)
                fd = os.open(media / '.mover-app.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                stack.callback(os.close, fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise MoveError('Unsafe drive lock file')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise MoveError('Another mover instance holds a drive lock')
            yield

    def move(self, request, progress=lambda **kw: None, cancelled=lambda: False):
        self.plan(request)
        with self.branch_locks():
            return self._move(request, progress, cancelled)

    def _move(self, request, progress=lambda **kw: None, cancelled=lambda: False):
        plan = self.plan(request)
        if request.get('reviewed_snapshot') is not None and json.dumps(plan['_snapshot'], sort_keys=True) != json.dumps(request['reviewed_snapshot'], sort_keys=True):
            raise MoveError('Source changed since review; review again')
        source = self.folder(plan['source'], plan['category'], plan['name'])
        dest = self.folder(plan['target'], plan['destination_category'], plan['name'])
        files, dirs = plan['_snapshot']
        total = plan['size']
        copied = 0
        hashes = {}
        def guard(rel=None, allow_missing=False):
            if cancelled():
                raise MoveError('Cancelled; remaining source data kept')
            self.check_drives()
            self.folder(plan['source'], plan['category'], plan['name'])
            self.folder(plan['target'], plan['destination_category'], plan['name'])
            if signature(source)[:2] != dirs['.'][:2]:
                raise MoveError('Source directory identity changed')
            if rel is not None:
                for base in (source, dest):
                    cur = base
                    device = base.stat().st_dev
                    for part in Path(rel).parts[:-1]:
                        cur /= part
                        if allow_missing and base == dest and not os.path.lexists(cur):
                            continue
                        st = cur.lstat()
                        if not stat.S_ISDIR(st.st_mode) or st.st_dev != device:
                            raise MoveError('Unsafe or replaced directory in move path')
            if locked(source, source.parent) and not plan['override_lock']:
                raise MoveError('Source was locked during move')
        guard()
        created_dirs = [dest] if not dest.exists() else []
        dest.mkdir(parents=True, exist_ok=True)
        for rel in sorted(dirs, key=lambda r: len(Path(r).parts)):
            guard(rel + '/__entry', allow_missing=True)
            directory = dest / rel
            if not directory.exists():
                created_dirs.append(directory)
            directory.mkdir(parents=True, exist_ok=True)
        for rel, expected in files.items():
            guard(rel)
            src, dst = source / rel, dest / rel
            if signature(src) != expected:
                raise MoveError(f'Source changed: {rel}')
            progress(phase='copying', current=rel, completed_bytes=copied, total_bytes=total)
            expected_hash = digest(src)
            hashes[rel] = expected_hash
            guard(rel)
            if os.path.lexists(dst):
                raise MoveError(f'Destination file appeared during move: {rel}')
            else:
                fd, tmp = tempfile.mkstemp(prefix='.mover-', dir=dst.parent)
                try:
                    with os.fdopen(fd, 'wb') as out, open(src, 'rb') as inp:
                        while True:
                            guard(rel)
                            block = inp.read(1024*1024)
                            if not block:
                                break
                            out.write(block)
                            progress(phase='copying', current=rel, completed_bytes=copied+out.tell(), total_bytes=total)
                        out.flush()
                        os.fsync(out.fileno())
                    shutil.copystat(src, tmp, follow_symlinks=False)
                    with open(tmp, 'rb') as metadata_file:
                        os.fsync(metadata_file.fileno())
                    if signature(src) != expected or digest(tmp) != expected_hash:
                        raise MoveError(f'Copy verification failed: {rel}')
                    # Exclusive publication: never overwrite an existing file.
                    os.link(tmp, dst)
                    dfd = os.open(dst.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                finally:
                    os.unlink(tmp)
            copied += expected[2]
            progress(phase='file_copied',current=rel,completed_bytes=copied,total_bytes=total)
        # Preserve metadata only on newly created directories; existing
        # existing directories retain their original permissions and ownership.
        for directory in reversed(created_dirs):
            source_dir = source / directory.relative_to(dest)
            owner = source_dir.stat()
            os.chown(directory, owner.st_uid, owner.st_gid, follow_symlinks=False)
            shutil.copystat(source_dir, directory, follow_symlinks=False)
        # Persist all directory entries, including empty directories, before
        # permitting any source cleanup.
        for rel in sorted(dirs, key=lambda r: len(Path(r).parts), reverse=True):
            guard(rel + '/__entry')
            dfd = os.open(dest / rel, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        for ancestor in (dest.parent, self.roots[plan['target']]):
            dfd = os.open(ancestor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        guard()
        progress(phase='verifying', completed_bytes=copied, total_bytes=total)
        if snapshot(source) != (files, dirs):
            raise MoveError('Source tree changed; source kept')
        for rel, expected in files.items():
            guard(rel)
            dst = dest / rel
            if dst.is_symlink() or not dst.is_file() or signature(source / rel) != expected or digest(dst) != hashes[rel] or digest(source / rel) != hashes[rel]:
                raise MoveError(f'Final verification failed: {rel}')
            progress(phase='file_verified',current=rel,completed_bytes=copied,total_bytes=total)
        # Never recursively delete. Late additions survive, and failed unlinks
        # are reported. External writers must be paused for guaranteed safety.
        progress(phase='cleaning', completed_bytes=copied, total_bytes=total)
        self.check_other_branches(plan, files, dirs)
        for rel, expected in files.items():
            guard(rel)
            if signature(source / rel) != expected or digest(source / rel) != hashes[rel] or (dest / rel).is_symlink() or digest(dest / rel) != hashes[rel]:
                raise MoveError(f'File changed before cleanup: {rel}')
            (source / rel).unlink()
            progress(phase='source_removed',current=rel,completed_bytes=copied,total_bytes=total)
        for rel in sorted(dirs, key=lambda r: len(Path(r).parts), reverse=True):
            guard(rel + '/__entry')
            try:
                (source / rel).rmdir()
                progress(phase='directory_removed',current=rel,completed_bytes=copied,total_bytes=total)
            except OSError as e:
                raise MoveError(f'Source cleanup incomplete; remaining entries kept: {e}') from e
        progress(phase='completed', completed_bytes=copied, total_bytes=total)
        return {k:v for k,v in plan.items() if not k.startswith('_')}
