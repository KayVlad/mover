"""Persistent, read-only task history and structured event timelines."""
import datetime as dt
import json
import secrets
from .engine import MoveError

def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()

class HistoryMixin:
    def init_history(self, db):
        db.executescript('''CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, kind TEXT, name TEXT, created TEXT, status TEXT, request TEXT, details TEXT);
            CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, created TEXT, level TEXT, message TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS events_task ON events(task_id,seq);
            CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created DESC,id DESC);''')
        for job in db.execute('SELECT * FROM jobs').fetchall():
            if not db.execute('SELECT 1 FROM tasks WHERE id=?',(job['id'],)).fetchone():
                request=json.loads(job['request'])
                self.create_task('move',request.get('category','')+'/'+request.get('name',''),request,
                                 status=job['status'],jid=job['id'],created=job['created'],details=json.loads(job['details']),db=db)
                self.event(job['id'],'info','Imported existing task; detailed events were not recorded by the previous version',db=db)
        for task in db.execute("SELECT id FROM tasks WHERE status IN ('running','queued')").fetchall():
            message='Server restarted; task interrupted. No automatic retry.'
            self.task_update(task['id'],'interrupted',db=db,error=message)
            self.event(task['id'],'error',message,db=db)
        for job in db.execute("SELECT id,details FROM jobs WHERE status IN ('running','queued')").fetchall():
            details=json.loads(job['details']);details['error']='Server restarted; inspect both branches before retrying'
            db.execute("UPDATE jobs SET status='interrupted',details=? WHERE id=?",(json.dumps(details),job['id']))

    def create_task(self, kind, name, request, status='running', jid=None, created=None, details=None, db=None):
        if db is None:
            with self.lock, self.db() as db:
                return self.create_task(kind,name,request,status,jid,created,details,db)
        jid=jid or secrets.token_hex(8)
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)',(jid,kind,name,created or utcnow(),status,json.dumps(request),json.dumps(details or {})))
        return jid

    def event(self, jid, level, message, db=None, **data):
        if db is None:
            with self.lock,self.db() as db:
                return self.event(jid,level,message,db,**data)
        db.execute('INSERT INTO events (task_id,created,level,message,data) VALUES (?,?,?,?,?)',
                   (jid,utcnow(),level,message,json.dumps(data)))

    def task_update(self, jid, status=None, db=None, **changes):
        if db is None:
            with self.lock,self.db() as db:
                return self.task_update(jid,status,db,**changes)
        row=db.execute('SELECT status,details FROM tasks WHERE id=?',(jid,)).fetchone()
        if row:
            details=json.loads(row['details']);details.update(changes)
            db.execute('UPDATE tasks SET status=?,details=? WHERE id=?',(status or row['status'],json.dumps(details),jid))

    @staticmethod
    def public_task(row):
        return dict(id=row['id'],kind=row['kind'],name=row['name'],created=row['created'],status=row['status'],
                    request={k:v for k,v in json.loads(row['request']).items() if k!='reviewed_snapshot'},details=json.loads(row['details']))

    def tasks(self, offset=0, limit=50, search='', status='', kind=''):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise MoveError('Invalid log page')
        if status and status not in ('queued','running','completed','failed','cancelled','interrupted'):
            raise MoveError('Unknown task status')
        if kind and kind not in ('move','review','schedule','batch','lock'):
            raise MoveError('Unknown task type')
        where,params=[],[]
        if search:
            where.append('(instr(lower(name),lower(?))>0 OR instr(lower(request),lower(?))>0 OR instr(lower(details),lower(?))>0)')
            params.extend([search,search,search])
        for field,value in [('status',status),('kind',kind)]:
            if value:where.append(field+'=?');params.append(value)
        clause=' WHERE '+' AND '.join(where) if where else ''
        with self.db() as db:
            total=db.execute('SELECT count(*) FROM tasks'+clause,params).fetchone()[0]
            rows=db.execute('SELECT * FROM tasks'+clause+' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',(*params,limit,offset)).fetchall()
        return dict(tasks=[self.public_task(r) for r in rows],total=total,offset=offset,has_more=offset+len(rows)<total)

    def task_detail(self, jid, after=0, limit=500):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=1000:
            raise MoveError('Invalid event page')
        with self.db() as db:
            row=db.execute('SELECT * FROM tasks WHERE id=?',(jid,)).fetchone()
            if not row:raise MoveError('Task not found')
            events=db.execute('SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?', (jid,after,limit+1)).fetchall()
            children=db.execute("SELECT * FROM tasks WHERE json_extract(request,'$.parent_task_id')=? ORDER BY created,id",(jid,)).fetchall()
        return dict(task=self.public_task(row),children=[self.public_task(r) for r in children],
                    events=[dict(seq=r['seq'],created=r['created'],level=r['level'],message=r['message'],data=json.loads(r['data'])) for r in events[:limit]],
                    has_more=len(events)>limit)

    def sync_batch(self, jid, db):
        request=json.loads(db.execute('SELECT request FROM jobs WHERE id=?',(jid,)).fetchone()[0])
        bid=request.get('batch_id')
        if not bid:return
        children=db.execute("SELECT id,status FROM jobs WHERE json_extract(request,'$.batch_id')=?",(bid,)).fetchall()
        statuses=[r['status'] for r in children]
        status=next((s for s in ('running','queued','interrupted','failed','cancelled') if s in statuses),'completed')
        self.task_update(bid,status,db=db,completed=sum(s=='completed' for s in statuses),total=len(statuses))
