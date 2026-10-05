import datetime as dt
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo
from .engine import Engine, MoveError, valid_category
from .history import HistoryMixin, utcnow
from .schedules import SchedulesMixin
from .batches import BatchesMixin

DEFAULT_SETTINGS = dict(enabled=False, frequency='daily', weekdays=[0,1,2,3,4,5,6], day=1,
                        time='03:00', timezone='Europe/Dublin', categories=[],
                        age_days=0, margin_gb=5, source='', target='', location_roles={}, max_folders=10, max_gb=0)

class Service(HistoryMixin, SchedulesMixin, BatchesMixin):
    def __init__(self, roots, state_dir, require_mounts=True, start_worker=True):
        self.engine = Engine(roots, require_mounts)
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        self.db_path = str(Path(state_dir) / 'mover.sqlite')
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.reviews = {}
        self.schedule_reviews = {}
        self.inventory_cache = []
        self.inventory_time = 0
        self.inventory_lock = threading.Lock()
        self.last_inventory_error = None
        with self.db() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT);'
                             'CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created TEXT, request TEXT, status TEXT, details TEXT);'
                             'CREATE TABLE IF NOT EXISTS runs (day TEXT PRIMARY KEY, created TEXT);')
            if 'result' not in {r['name'] for r in db.execute('PRAGMA table_info(runs)')}:
                db.execute('ALTER TABLE runs ADD COLUMN result TEXT')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (json.dumps({**DEFAULT_SETTINGS, "categories": self.engine.categories()}),))
            stored = json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
            migrated = {**DEFAULT_SETTINGS, **stored}
            migrated['location_roles'] = {name:stored.get('location_roles',{}).get(name,'unassigned') for name in self.engine.roots}
            if 'source' not in stored:
                migrated.update(enabled=False, source='')
            if migrated['source'] and migrated['source'] not in self.engine.roots:
                migrated.update(enabled=False, source='')
            if migrated['target'] not in ('', 'auto', *self.engine.roots):
                migrated.update(enabled=False, target='')
            if migrated['target'] == 'auto' and not any(role=='cold' for role in migrated['location_roles'].values()):
                migrated['enabled'] = False
            db.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(migrated),))
            self.init_schedules(db)
            self.init_history(db)
        self.apply_settings()
        if start_worker:
            threading.Thread(target=self.worker, daemon=True).start()
            threading.Thread(target=self.scheduler, daemon=True).start()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        with self.db() as db:
            return json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])

    def global_settings(self):
        with self.db() as db:
            return json.loads(db.execute("SELECT value FROM meta WHERE key='global_settings'").fetchone()[0])

    def apply_settings(self):
        settings = self.global_settings()
        self.engine.margin = settings['margin_gb'] * 1024**3
        self.engine.location_roles = dict(settings['location_roles'])

    def validate_settings(self, data):
        if set(data) != set(DEFAULT_SETTINGS):
            raise MoveError('Settings fields do not match expected schema')
        if type(data['enabled']) is not bool:
            raise MoveError('Enabled must be a boolean')
        if data['frequency'] not in ('daily','weekdays','monthly'):
            raise MoveError('Unknown frequency')
        try:
            parsed = dt.datetime.strptime(data['time'], '%H:%M')
            if parsed.strftime('%H:%M') != data['time']:
                raise ValueError()
            ZoneInfo(data['timezone'])
        except Exception:
            raise MoveError('Invalid time or timezone')
        if not isinstance(data['weekdays'], list) or not data['weekdays'] or any(type(d) is not int or d not in range(7) for d in data['weekdays']):
            raise MoveError('Select valid weekdays')
        if not isinstance(data['categories'], list) or (data['enabled'] and not data['categories']) or any(not valid_category(c) for c in data['categories']):
            raise MoveError('Select valid categories')
        roles = data['location_roles']
        if not isinstance(roles, dict) or set(roles) != set(self.engine.roots) or any(role not in ('unassigned','cache','cold') for role in roles.values()):
            raise MoveError('Assign every configured location a valid role: unassigned, cache, or cold')
        for field in ('source', 'target'):
            allowed = ('', *self.engine.roots) if field=='source' else ('', 'auto', *self.engine.roots)
            if not isinstance(data[field], str) or data[field] not in allowed:
                raise MoveError(f'Unknown {field} location')
        if data['source'] and data['source'] == data['target']:
            raise MoveError('Scheduled source and destination must be different locations')
        if data['enabled']:
            self.validate_route(data)
        for key, low, high in [('age_days',0,36500), ('margin_gb',0,100000), ('max_folders',1,10000), ('max_gb',0,100000), ('day',1,28)]:
            if type(data[key]) is not int or not low <= data[key] <= high:
                raise MoveError(f'Invalid {key}: expected {low}–{high}')
        return data

    def save_settings(self,data):
        self.validate_settings(data)
        with self.lock:
            with self.db() as db:
                db.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(data),))
                db.execute("UPDATE meta SET value=? WHERE key='global_settings'",(json.dumps({k:data[k] for k in ('location_roles','margin_gb')}),))
                # Compatibility with the previous single-policy API.
                if db.execute("SELECT 1 FROM schedules WHERE id='default'").fetchone():
                    policy={k:v for k,v in data.items() if k!='location_roles'}
                    db.execute("UPDATE schedules SET data=? WHERE id='default'",(json.dumps(policy),))
            self.apply_settings()
        return data

    def save_global_settings(self,data):
        if set(data) != {'location_roles','margin_gb'}:raise MoveError('Invalid global settings fields')
        with self.lock:
            settings={**self.settings(),**data}
            # Changing global roles does not silently toggle schedules. Their
            # next run logs an error if an automatic route is no longer valid.
            self.validate_settings({**settings,'enabled':False})
            with self.db() as db:
                db.execute("UPDATE meta SET value=? WHERE key='global_settings'",(json.dumps(data),))
                legacy=self.settings();legacy['location_roles']=data['location_roles']
                db.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(legacy),))
            self.apply_settings()
        return data

    def jobs(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100').fetchall()
        return [dict(id=r['id'], created=r['created'], request={k:v for k,v in json.loads(r['request']).items() if k!='reviewed_snapshot'}, status=r['status'], **json.loads(r['details'])) for r in rows]

    def set_folder_lock(self, request):
        desired = request.get('locked')
        if type(desired) is not bool:
            raise MoveError('Locked must be a boolean')
        with self.lock:
            self.engine.check_drives()
            folder = self.engine.folder(request['drive'], request['category'], request['name'])
            if not folder.is_dir():
                raise MoveError('Folder does not exist')
            with self.db() as db:
                for job in db.execute("SELECT request FROM jobs WHERE status IN ('queued','running')"):
                    move = json.loads(job['request'])
                    if move['name'] == request['name'] and (
                        (move['source'] == request['drive'] and move['category'] == request['category']) or
                        (move.get('target') == request['drive'] and move.get('destination_category', move['category']) == request['category'])):
                        raise MoveError('Cancel this folder’s active job before changing its lock')
            with self.engine.branch_locks():
                folder = self.engine.folder(request['drive'], request['category'], request['name'])
                if not folder.is_dir():
                    raise MoveError('Folder does not exist')
                if not desired and os.path.lexists(folder.parent / '.mvlock'):
                    raise MoveError('Locked by the parent category; its lock cannot be removed with the folder toggle')
                path = folder / '.mvlock'
                if os.path.lexists(path):
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise MoveError('Unsafe .mvlock entry; only a regular, unlinked lock file can be toggled')
                    if not desired:
                        path.unlink()
                elif desired:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.inventory_time = 0
                # A lock change alters the source manifest; old reviews expire.
                self.reviews.clear()
                self.schedule_reviews.clear()
            return dict(locked=desired, folder_locked=desired)

    def review(self, request):
        task=self.create_task('review',request.get('category','')+'/'+request.get('name','Folder'),request)
        try:
            if type(request.get('override_lock',False)) is not bool:raise MoveError('Lock override must be a boolean')
            with self.lock:
                plan=self.engine.plan(request)
                token=secrets.token_urlsafe(24)
                self.reviews={k:v for k,v in self.reviews.items() if v[0]>time.time()}
                self.reviews[token]=(time.time()+300,plan)
            self.event(task,'info','Move review passed',destination=plan['destination'],files=plan['files'],size=plan['size'])
            self.task_update(task,'completed',finished=utcnow())
            return dict(review_token=token,**{k:v for k,v in plan.items() if not k.startswith('_')})
        except Exception as e:
            self.event(task,'error',str(e),error_type=type(e).__name__)
            self.task_update(task,'failed',error=str(e),finished=utcnow())
            raise

    def enqueue_review(self, token):
        with self.lock:
            if token not in self.reviews:raise MoveError('Review expired; review the move again')
            expiry,plan=self.reviews.pop(token)
            if time.time()>expiry:raise MoveError('Review expired; review the move again')
            try:
                return self.enqueue_batch(plan) if isinstance(plan,list) else self.enqueue(plan)
            except Exception as e:
                requests=plan if isinstance(plan,list) else [plan]
                task=self.create_task('batch' if isinstance(plan,list) else 'move','Queue failed',dict(items=[{k:v for k,v in p.items() if not k.startswith('_')} for p in requests]),status='failed',details=dict(error=str(e)))
                self.event(task,'error',str(e),error_type=type(e).__name__)
                raise

    def enqueue(self,request,db=None):
        if db is None:
            with self.lock,self.db() as db:return self.enqueue(request,db)
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') AND json_extract(request,'$.source')=? AND json_extract(request,'$.category')=? AND json_extract(request,'$.name')=?",
                      (request['source'],request['category'],request['name'])).fetchone():
            raise MoveError('This folder already has an active job')
        request=dict(request)
        if '_snapshot' in request:request['reviewed_snapshot']=request['_snapshot']
        request={k:v for k,v in request.items() if not k.startswith('_')}
        jid=secrets.token_hex(8);created=utcnow()
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(jid,created,json.dumps(request),'queued','{}'))
        self.create_task('move',request['category']+'/'+request['name'],request,status='queued',jid=jid,created=created,db=db)
        self.event(jid,'info','Move queued',db=db,source=request['source'],target=request.get('target'),category=request.get('destination_category',request['category']),margin_gb=request.get('margin_gb'))
        if request.get('override_lock'):self.event(jid,'warning','Manual lock override enabled for this move',db=db)
        if request.get('parent_task_id'):self.event(request['parent_task_id'],'info','Move queued',db=db,job_id=jid,folder=request['category']+'/'+request['name'],target=request.get('target'))
        return dict(id=jid)

    def cancel(self, jid):
        with self.lock, self.db() as db:
            row = db.execute('SELECT status,details FROM jobs WHERE id=?', (jid,)).fetchone()
            if not row or row['status'] not in ('queued','running'):
                raise MoveError('Job is not active')
            details = json.loads(row['details'])
            details['cancel_requested'] = True
            db.execute('UPDATE jobs SET status=?,details=? WHERE id=?', ('cancelled' if row['status']=='queued' else 'running', json.dumps(details), jid))
            self.task_update(jid,'cancelled' if row['status']=='queued' else 'running',db=db,cancel_requested=True)
            self.event(jid,'warning','Cancellation requested; remaining source entries will be kept',db=db)
            self.sync_batch(jid,db)
        return dict(ok=True)

    def update(self, jid, status=None, **changes):
        with self.lock, self.db() as db:
            row = db.execute('SELECT status,details FROM jobs WHERE id=?', (jid,)).fetchone()
            details = json.loads(row['details'])
            details.update(changes)
            db.execute('UPDATE jobs SET status=?,details=? WHERE id=?', (status or row['status'], json.dumps(details), jid))
            self.task_update(jid,status,db=db,**changes)
            if status:self.sync_batch(jid,db)

    def cancelled(self, jid):
        with self.db() as db:
            row = db.execute('SELECT details FROM jobs WHERE id=?', (jid,)).fetchone()
        return self.stop.is_set() or json.loads(row[0]).get('cancel_requested', False)

    def process_one(self):
        with self.lock, self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return False
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (row['id'],))
        jid = row['id']
        self.update(jid,'running',started=utcnow())
        self.event(jid,'info','Move started; checking source, destination, locks, and space')
        last_update = [0]
        last_event = [None]
        def progress(**kw):
            now = time.monotonic()
            phase=kw.get('phase');key=(phase,kw.get('current'))
            if key != last_event[0]:
                labels={'copying':'Copying file','file_copied':'File copied and published','verifying':'Verifying source tree and destination content','file_verified':'File content verified','cleaning':'Removing verified source entries','source_removed':'Source file removed','directory_removed':'Empty source directory removed','completed':'Move completed'}
                self.event(jid,'info',labels.get(phase,phase or 'Progress'),**kw)
                last_event[0]=key
            if now-last_update[0] > .25 or kw.get('phase') != 'copying':
                self.update(jid, **kw)
                last_update[0] = now
        try:
            self.engine.move(json.loads(row['request']), progress, lambda: self.cancelled(jid))
            self.update(jid, 'completed',finished=utcnow())
            self.inventory_time = 0
        except Exception as e:
            status='cancelled' if self.cancelled(jid) else 'failed'
            self.event(jid,'warning' if status=='cancelled' else 'error',str(e),error_type=type(e).__name__)
            self.update(jid,status,error=str(e),error_type=type(e).__name__,finished=utcnow())
        return True

    def worker(self):
        while not self.stop.is_set():
            try:
                if not self.process_one():
                    self.stop.wait(.5)
            except Exception as e:
                self.last_inventory_error = str(e)
                self.stop.wait(2)

    def validate_route(self, settings):
        if not settings['source'] or settings['source'] not in self.engine.roots:
            raise MoveError('Choose a source location for the schedule')
        if not settings['target'] or settings['target'] not in ('auto', *self.engine.roots):
            raise MoveError('Choose a destination location for the schedule')
        if settings['target'] == 'auto' and not any(name != settings['source'] and role=='cold' for name,role in settings['location_roles'].items()):
            raise MoveError('Assign another location as cold storage to use automatic destination selection')
        if settings['source'] == settings['target']:
            raise MoveError('Scheduled source and destination must be different locations')

    def eligible(self, settings):
        return [r for r in self.engine.inventory() if r['drive']==settings['source'] and r['category'] in settings['categories']
                and not r['locked'] and not r['error'] and r['age'] is not None and r['age'] >= settings['age_days']]

    def policy(self, execute=False, schedule_id=None, parent_task_id=None, reviewed_plans=None):
        settings = self.settings() if schedule_id is None else {**{k:v for k,v in self.schedule(schedule_id).items() if k not in ('id','name')},'location_roles':self.global_settings()['location_roles']}
        self.validate_route(settings)
        result, errors, used = [], [], 0
        rows=[r for r in self.engine.inventory() if r['drive']==settings['source'] and r['category'] in settings['categories']]
        for row in rows:
            folder=row['category']+'/'+row['name']
            if row['error']:
                errors.append(dict(folder=folder,error=row['error']))
                continue
            reason='locked' if row['locked'] else 'empty' if row['age'] is None else 'too new' if row['age']<settings['age_days'] else None
            if reason:
                if parent_task_id:self.event(parent_task_id,'info','Folder skipped: '+reason,folder=folder,age=row['age'])
                continue
            if len(result) >= settings['max_folders']:
                break
            if settings['max_gb'] and used+row['size'] > settings['max_gb'] * 1024**3:
                continue
            request = dict(source=settings['source'], category=row['category'], name=row['name'], target=settings['target'], destination_category=row['category'],margin_gb=settings['margin_gb'])
            try:
                plan = self.engine.plan(request)
                public = {k:v for k,v in plan.items() if not k.startswith('_')}
                if reviewed_plans is not None:reviewed_plans.append(plan)
                if execute:
                    if parent_task_id:plan['parent_task_id']=parent_task_id
                    if schedule_id:plan['schedule_id']=schedule_id
                    public.update(self.enqueue(plan))
                result.append(public)
                used += row['size']
            except (OSError, MoveError) as e:
                errors.append(dict(folder=f"{row['category']}/{row['name']}", error=str(e)))
        return dict(folders=result, errors=errors, total_bytes=used)

    @staticmethod
    def matches(settings, date):
        return settings['frequency']=='daily' or (settings['frequency']=='weekdays' and date.weekday() in settings['weekdays']) or (settings['frequency']=='monthly' and date.day==settings['day'])

    def scheduler(self):
        while not self.stop.is_set():
            try:
                self.scheduler_tick()
            except Exception as e:
                self.last_inventory_error = str(e)
            self.stop.wait(10)

    def state(self, refresh=False):
        import shutil
        try:
            self.engine.check_drives()
            with self.inventory_lock:
                if refresh or time.monotonic()-self.inventory_time > 30:
                    self.inventory_cache = self.engine.inventory()
                    self.inventory_time = time.monotonic()
                folders = self.inventory_cache
            self.last_inventory_error = None
        except (OSError, MoveError) as e:
            folders = []
            self.last_inventory_error = str(e)
        drives = []
        for name, path in self.engine.roots.items():
            try:
                usage = shutil.disk_usage(path)
                drives.append(dict(name=name, path=str(path), free=usage.free, total=usage.total, used=usage.used, role=self.global_settings()['location_roles'].get(name,'unassigned')))
            except OSError:
                drives.append(dict(name=name, path=str(path), free=0, total=0, used=0, role=self.global_settings()['location_roles'].get(name,'unassigned')))
        with self.db() as db:
            last_run = db.execute('SELECT created,result FROM runs ORDER BY created DESC LIMIT 1').fetchone()
        schedule_run = dict(created=last_run['created'], **json.loads(last_run['result'] or '{}')) if last_run else None
        with self.db() as db:
            active_jobs=db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        return dict(active_jobs=active_jobs,global_settings=self.global_settings(),schedules=self.schedules(),schedule_run=schedule_run, folders=folders, drives=drives, jobs=self.jobs(), settings=self.settings(), next_run=self.next_run(),
                    error=self.last_inventory_error, demo=not self.engine.require_mounts, categories=self.engine.categories())

class Handler(BaseHTTPRequestHandler):
    server_version = 'Mover'
    def log_message(self, fmt, *args):
        pass

    def respond(self, value, status=200):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def authenticated(self):
        token = self.server.token
        return not token or hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+token)

    def do_GET(self):
        parsed=urlsplit(self.path)
        query=parse_qs(parsed.query)
        if parsed.path == '/api/tasks' or parsed.path.startswith('/api/tasks/'):
            if not self.authenticated():return self.respond(dict(error='Authentication required'),401)
            try:
                if parsed.path=='/api/tasks':
                    value=self.server.service.tasks(offset=int(query.get('offset',['0'])[0]),limit=int(query.get('limit',['50'])[0]),search=query.get('search',[''])[0],status=query.get('status',[''])[0],kind=query.get('kind',[''])[0])
                else:value=self.server.service.task_detail(parsed.path[len('/api/tasks/'):],after=int(query.get('after',['0'])[0]))
                return self.respond(value)
            except (MoveError,ValueError,TypeError) as e:return self.respond(dict(error=str(e)),400)
        if self.path == '/health':
            return self.respond(dict(ok=True))
        if self.path == '/api/state':
            if not self.authenticated():
                return self.respond(dict(error='Authentication required'), 401)
            try:
                return self.respond(self.server.service.state())
            except Exception as e:
                return self.respond(dict(error=str(e)), 503)
        if self.path not in ('/', '/app.js', '/style.css'):
            return self.respond(dict(error='Not found'), 404)
        path = Path(__file__).parent / 'static' / ('index.html' if self.path=='/' else self.path[1:])
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', {'html':'text/html', 'js':'application/javascript', 'css':'text/css'}[path.suffix[1:]])
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if not self.authenticated():
            return self.respond(dict(error='Authentication required'), 401)
        # JSON-only writes plus strict same-origin checks prevent browser CSRF.
        origin = self.headers.get('Origin')
        if origin and origin not in ('http://'+self.headers.get('Host',''), 'https://'+self.headers.get('Host','')):
            return self.respond(dict(error='Cross-origin requests refused'), 403)
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            return self.respond(dict(error='JSON required'), 415)
        try:
            size = int(self.headers.get('Content-Length',0))
            if not 0 < size <= 65536:
                raise MoveError('Invalid request size')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise MoveError('Request must be an object')
            service = self.server.service
            if self.path == '/api/scan':
                value = service.state(refresh=True)
            elif self.path == '/api/batch/review':
                value=service.review_batch(data['items'])
            elif self.path == '/api/batch/lock':
                value=service.set_batch_locks(data)
            elif self.path == '/api/global-settings':
                value=service.save_global_settings(data)
            elif self.path == '/api/schedules/save':
                value=service.save_schedule(data)
            elif self.path == '/api/schedules/toggle':
                value=service.toggle_schedule(data['id'],data['enabled'])
            elif self.path == '/api/schedules/delete':
                value=service.delete_schedule(data['id'])
            elif self.path == '/api/lock':
                value = service.set_folder_lock(data)
            elif self.path == '/api/review':
                value = service.review(data)
            elif self.path == '/api/move':
                value = service.enqueue_review(data['review_token'])
            elif self.path == '/api/cancel':
                value = service.cancel(data['id'])
            elif self.path == '/api/settings':
                value = service.save_settings(data)
            elif self.path == '/api/preview':
                value = service.review_schedule(data.get('id'))
            elif self.path == '/api/run-now':
                value = service.confirm_schedule_review(data.get('id'),data.get('review_token'))
            else:
                return self.respond(dict(error='Not found'), 404)
            self.respond(value)
        except (MoveError, OSError, ValueError, KeyError, TypeError) as e:
            self.respond(dict(error=str(e)), 400)
        except Exception:
            self.respond(dict(error='Internal error; inspect server logs'), 500)


def configured_roots(demo=False, desktop=None):
    raw = os.environ.get('MOVER_ROOTS')
    if raw is not None:
        try:
            roots = json.loads(raw)
        except ValueError:
            raise MoveError('MOVER_ROOTS must be a JSON object of location names and absolute paths')
        if not isinstance(roots, dict) or len(roots) < 1 or any(
            not isinstance(name,str) or not name.strip() or name=='auto' or
            not isinstance(path,str) or not Path(path).is_absolute()
            for name,path in roots.items()):
            raise MoveError('Configure at least one named location with absolute paths')
        return roots
    desktop = desktop or Path.home() / 'Desktop'
    # These are default location names, not storage roles.
    return {d: os.environ.get('MOVER_'+d.upper(), str(desktop / ('mover-test-'+d) / 'data' / 'Media') if demo else '/mnt/'+d+'/data/Media')
            for d in ('ssd1','drive1','drive2')}


def main():
    demo = os.environ.get('MOVER_DEMO','0') == '1'
    desktop = Path.home() / 'Desktop'
    roots = configured_roots(demo, desktop)
    token = os.environ.get('MOVER_TOKEN','')
    if not demo and len(token) < 24:
        raise SystemExit('Set MOVER_TOKEN to a random token of at least 24 characters')
    state_dir = Path(os.environ.get('MOVER_STATE',str(Path.cwd() / '.state')))
    state_dir.mkdir(parents=True, exist_ok=True)
    # Also prevents independent backend instances from moving concurrently.
    import fcntl
    lock_file = open(state_dir / 'worker.lock', 'a')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another mover instance uses this state directory')
    service = Service(roots, state_dir, require_mounts=not demo)
    server = ThreadingHTTPServer((os.environ.get('MOVER_BIND','127.0.0.1'), int(os.environ.get('MOVER_PORT','8080'))), Handler)
    server.service, server.token = service, token
    print(f'Mover listening on {server.server_address}; demo={demo}', flush=True)
    try:
        server.serve_forever()
    finally:
        service.stop.set()
        server.server_close()

if __name__ == '__main__':
    main()
