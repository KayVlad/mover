"""Independent named schedules, sharing one move worker."""
import datetime as dt
import json
import secrets
import time
from zoneinfo import ZoneInfo
from .engine import MoveError
from .history import utcnow

class SchedulesMixin:
    def init_schedules(self, db):
        db.executescript('CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY,name TEXT,data TEXT);'
                         'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT);')
        if not db.execute("SELECT 1 FROM meta WHERE key='global_settings'").fetchone():
            settings=json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
            db.execute("INSERT INTO meta VALUES ('global_settings',?)",(json.dumps({k:settings[k] for k in ('location_roles','margin_gb')}),))
        if not db.execute("SELECT 1 FROM meta WHERE key='multiple_schedules'").fetchone():
            settings=json.loads(db.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
            policy={k:v for k,v in settings.items() if k!='location_roles'}
            db.execute('INSERT OR IGNORE INTO schedules VALUES (?,?,?)',('default','Default schedule',json.dumps(policy)))
            db.execute("UPDATE runs SET day='default:'||day")
            db.execute("INSERT INTO meta VALUES ('multiple_schedules','1')")

    def schedule(self, sid):
        with self.db() as db:
            row=db.execute('SELECT * FROM schedules WHERE id=?',(sid,)).fetchone()
        if not row:raise MoveError('Schedule not found')
        return dict(id=row['id'],name=row['name'],**json.loads(row['data']))

    def schedules(self):
        with self.db() as db:
            rows=db.execute('SELECT * FROM schedules ORDER BY name,id').fetchall()
        return [dict(id=r['id'],name=r['name'],**json.loads(r['data']),next_run=self.next_run(json.loads(r['data']),r['id'])) for r in rows]

    def save_schedule(self, data):
        from .server import DEFAULT_SETTINGS
        expected=set(DEFAULT_SETTINGS)-{'location_roles'}
        if set(data)-{'id','name'} != expected:
            raise MoveError('Schedule fields do not match expected schema')
        name=data.get('name')
        if not isinstance(name,str) or not name.strip() or len(name)>80:
            raise MoveError('Give the schedule a name of 1–80 characters')
        sid=data.get('id')
        if sid is not None and (not isinstance(sid,str) or not sid):raise MoveError('Invalid schedule id')
        policy={k:data[k] for k in expected}
        with self.lock:
            combined={**policy,'location_roles':self.global_settings()['location_roles']}
            self.validate_settings(combined)
            # Even disabled saved schedules must have explicit valid routes.
            self.validate_route(combined)
            with self.db() as db:
                if sid and not db.execute('SELECT 1 FROM schedules WHERE id=?',(sid,)).fetchone():
                    raise MoveError('Schedule not found')
                sid=sid or secrets.token_hex(8)
                db.execute('INSERT INTO schedules VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,data=excluded.data',
                           (sid,name.strip(),json.dumps(policy)))
                if sid=='default':
                    db.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(combined),))
            self.apply_settings()
        return self.schedule(sid)

    def toggle_schedule(self, sid, enabled):
        if type(enabled) is not bool:raise MoveError('Enabled must be a boolean')
        with self.lock:
            schedule=self.schedule(sid)
            policy={k:v for k,v in schedule.items() if k not in ('id','name')}
            if enabled:self.validate_settings({**policy,'enabled':True,'location_roles':self.global_settings()['location_roles']})
            policy['enabled']=enabled
            with self.db() as db:
                db.execute('UPDATE schedules SET data=? WHERE id=?',(json.dumps(policy),sid))
                if sid=='default':
                    legacy={**self.settings(),'enabled':enabled}
                    db.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(legacy),))
        return self.schedule(sid)

    def delete_schedule(self,sid):
        with self.lock,self.db() as db:
            if not db.execute('SELECT 1 FROM schedules WHERE id=?',(sid,)).fetchone():raise MoveError('Schedule not found')
            db.execute('DELETE FROM schedules WHERE id=?',(sid,))
            if sid=='default':
                legacy=self.settings();legacy['enabled']=False
                db.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(legacy),))
        return dict(ok=True)

    def next_run(self, settings=None, sid='default'):
        settings=settings or self.settings()
        if not settings['enabled']:return None
        now=dt.datetime.now(ZoneInfo(settings['timezone']))
        with self.db() as db:ran={r[0] for r in db.execute('SELECT day FROM runs')}
        hour,minute=map(int,settings['time'].split(':'))
        for offset in range(370):
            date=now.date()+dt.timedelta(days=offset)
            candidate=dt.datetime.combine(date,dt.time(hour,minute),tzinfo=now.tzinfo)
            key=f"{sid}:{settings['timezone']}:{date}"
            if candidate>now and self.matches(settings,date) and key not in ran:return candidate.isoformat()
        return None

    def scheduler_tick(self,now=None):
        for schedule in self.schedules():
            with self.lock:
                # Reload so a concurrent disable/delete cannot launch a stale rule.
                try:current=self.schedule(schedule['id'])
                except MoveError:continue
                if not current['enabled']:continue
                local=(now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(current['timezone']))
                if local.strftime('%H:%M') != current['time'] or not self.matches(current,local.date()):continue
                key=f"{current['id']}:{current['timezone']}:{local.date()}"
                with self.db() as db:
                    if db.execute('SELECT 1 FROM runs WHERE day=?',(key,)).fetchone():continue
                    db.execute('INSERT INTO runs (day,created) VALUES (?,?)',(key,local.isoformat()))
                self.run_schedule(current,run_key=key)

    def review_schedule(self, sid):
        with self.lock:
            schedule = self.schedule(sid)
            plans = []
            result = self.policy(schedule_id=sid, reviewed_plans=plans)
            token = None
            if plans:
                self.validate_reviewed_plans(plans, "schedule")
                token = secrets.token_urlsafe(24)
                self.schedule_reviews = {k:v for k,v in self.schedule_reviews.items() if v[0] > time.time()}
                self.schedule_reviews[token] = (time.time()+300, schedule, self.global_settings()['location_roles'], plans, result['errors'])
            return dict(review_token=token, **result)

    def confirm_schedule_review(self, sid, token):
        with self.lock:
            if not isinstance(token,str) or token not in self.schedule_reviews:
                raise MoveError('Schedule review expired; preview the schedule again')
            expiry, schedule, roles, plans, errors = self.schedule_reviews.pop(token)
            if time.time() > expiry:
                raise MoveError('Schedule review expired; preview the schedule again')
            if sid != schedule['id'] or self.schedule(sid) != schedule or self.global_settings()['location_roles'] != roles:
                raise MoveError('Schedule or location roles changed since review; preview again')
            # One transaction: validate every reviewed folder and aggregate space,
            # then queue exactly these plans, without rerunning policy selection.
            with self.db() as db:
                self.validate_reviewed_plans(plans, "schedule")
                task = self.create_task('schedule', schedule['name'],
                    dict(schedule_id=sid, policy=schedule, trigger='manual'), db=db)
                folders = []
                for plan in plans:
                    job = self.enqueue({**plan, 'schedule_id':sid, 'parent_task_id':task}, db=db)
                    folders.append({**{k:v for k,v in plan.items() if not k.startswith('_')}, **job})
                result = dict(folders=folders, errors=errors, total_bytes=sum(p['size'] for p in plans))
                self.event(task, 'info', 'Queued exactly the reviewed folders', db=db, total=len(folders))
                for error in errors:
                    self.event(task, 'error', error['error'], db=db, folder=error['folder'])
                self.task_update(task, 'failed' if errors else 'completed', db=db, result=result, finished=utcnow())
            return dict(task_id=task, **result)

    def run_schedule(self,schedule,run_key=None):
        with self.lock:
            return self._run_schedule(self.schedule(schedule['id']),run_key)

    def _run_schedule(self, schedule, run_key=None):
        sid=schedule['id']
        task=self.create_task('schedule',schedule['name'],dict(schedule_id=sid,policy=schedule,trigger='timer' if run_key else 'manual'))
        self.event(task,'info','Scheduled run started',source=schedule['source'],target=schedule['target'])
        try:
            result=self.policy(execute=True,schedule_id=sid,parent_task_id=task)
            status='failed' if result['errors'] else 'completed'
            for error in result['errors']:self.event(task,'error',error['error'],folder=error['folder'])
            self.event(task,'info',f"Queued {len(result['folders'])} move(s); {len(result['errors'])} error(s)",total_bytes=result['total_bytes'])
            self.task_update(task,status,result=result,finished=utcnow())
        except Exception as e:
            result=dict(folders=[],errors=[dict(folder=schedule['name'],error=str(e))],total_bytes=0)
            self.event(task,'error',str(e),error_type=type(e).__name__)
            self.task_update(task,'failed',error=str(e),finished=utcnow())
        if run_key:
            with self.db() as db:db.execute('UPDATE runs SET result=? WHERE day=?',(json.dumps(result),run_key))
        return dict(task_id=task,**result)
