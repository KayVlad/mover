"""Review complete batches and enqueue them atomically."""
import json
import secrets
import shutil
import time
from .engine import MoveError
from .history import utcnow

class BatchesMixin:
    def review_batch(self, items):
        if not isinstance(items,list) or not 1<=len(items)<=100 or any(not isinstance(r,dict) for r in items):
            raise MoveError('Select 1–100 folders')
        with self.lock:
            tid=self.create_task('review',f'Batch review · {len(items)} folders',dict(items=items))
            plans,errors,seen=[],[],set()
            for index,request in enumerate(items):
                try:
                    if type(request.get('override_lock',False)) is not bool:raise MoveError('Lock override must be a boolean')
                    key=(request['source'],request['category'],request['name'])
                    if key in seen:raise MoveError('Folder selected more than once')
                    seen.add(key)
                    plan=self.engine.plan(request)
                    plans.append((index,plan))
                    self.event(tid,'info','Folder review passed',index=index,folder='/'.join(key),destination=plan['destination'])
                except (OSError,MoveError,KeyError,TypeError,ValueError) as e:
                    errors.append(dict(index=index,folder=request.get('name','Unknown folder'),error=str(e)))
            nodes={}
            for index,plan in plans:
                files,dirs=plan['_snapshot']
                for kind,entries in [('directory',dirs),('file',files)]:
                    for rel in entries:
                        key=(plan['destination_category'],plan['name'],rel)
                        if key in nodes and (kind=='file' or nodes[key][0]=='file'):
                            errors.append(dict(index=index,folder=plan['name'],error='Batch destination collision: '+ '/'.join(key)))
                        else:nodes[key]=(kind,index)
                for other_index,other in plans:
                    if index != other_index and (plan['target'],plan['destination_category'],plan['name']) == (other['source'],other['category'],other['name']):
                        errors.append(dict(index=index,folder=plan['name'],error='Destination is another selected source folder; split this into separate batches'))
            targets={}
            for index,plan in plans:
                entry=targets.setdefault(plan['target'],dict(size=0,margin=0,indices=[]))
                entry['size']+=plan['size'];entry['margin']=max(entry['margin'],plan['margin_gb']*1024**3);entry['indices'].append(index)
            for target,entry in targets.items():
                if shutil.disk_usage(self.engine.roots[target]).free < entry['size']+entry['margin']:
                    for index in entry['indices']:
                        errors.append(dict(index=index,folder=items[index]['name'],error=f'Insufficient free space for the complete batch on {target} including margin'))
            public=[dict(index=index,**{k:v for k,v in p.items() if not k.startswith('_')}) for index,p in plans]
            if errors:
                for error in errors:self.event(tid,'error',error['error'],index=error['index'],folder=error['folder'])
                self.task_update(tid,'failed',errors=errors,finished=utcnow())
                return dict(task_id=tid,items=public,errors=errors,review_token=None)
            token=secrets.token_urlsafe(24)
            self.reviews={k:v for k,v in self.reviews.items() if v[0]>time.time()}
            self.reviews[token]=(time.time()+300,[plan for _,plan in plans])
            self.event(tid,'info','Batch review complete; awaiting confirmation',files=sum(p['files'] for _,p in plans))
            self.task_update(tid,'completed',finished=utcnow())
            return dict(task_id=tid,items=public,errors=[],review_token=token)

    def validate_reviewed_plans(self, plans, review_kind="batch"):
        for plan in plans:
            current=self.engine.plan(plan)
            if json.dumps(current['_snapshot'],sort_keys=True)!=json.dumps(plan['_snapshot'],sort_keys=True):
                raise MoveError(f'Folder changed since {review_kind} review: '+plan['name'])
        targets={}
        for plan in plans:
            entry=targets.setdefault(plan['target'],dict(size=0,margin=0))
            entry['size']+=plan['size'];entry['margin']=max(entry['margin'],plan['margin_gb']*1024**3)
        for target,entry in targets.items():
            if shutil.disk_usage(self.engine.roots[target]).free < entry['size']+entry['margin']:
                raise MoveError('Insufficient free space for the complete batch on '+target)

    def enqueue_batch(self, plans):
        with self.lock,self.db() as db:
            self.validate_reviewed_plans(plans)
            # The whole transaction rolls back if any item already has a job.
            batch=self.create_task('batch',f'Batch move · {len(plans)} folders',dict(items=[{k:v for k,v in p.items() if not k.startswith('_')} for p in plans]),status='queued',db=db)
            jobs=[]
            for plan in plans:
                request={**plan,'batch_id':batch,'parent_task_id':batch}
                jobs.append(self.enqueue(request,db=db))
            self.event(batch,'info',f'Queued {len(jobs)} move(s)',db=db,job_ids=[j['id'] for j in jobs])
            self.task_update(batch,db=db,total=len(jobs),completed=0)
            return dict(id=batch,jobs=jobs)

    def set_batch_locks(self, data):
        items=data.get('items');desired=data.get('locked')
        if type(desired) is not bool or not isinstance(items,list) or not 1<=len(items)<=100 or any(not isinstance(r,dict) for r in items):
            raise MoveError('Provide 1–100 folders and a boolean lock state')
        result,errors=[],[]
        task=self.create_task('lock',f"{'Lock' if desired else 'Unlock'} · {len(items)} folders",dict(items=items,locked=desired))
        for index,item in enumerate(items):
            try:
                self.set_folder_lock({**item,'locked':desired})
                result.append(dict(index=index,name=item['name']))
                self.event(task,'info','Folder locked' if desired else 'Folder unlocked',folder=item['name'],drive=item['drive'],category=item['category'])
            except (MoveError,OSError,KeyError,TypeError) as e:
                errors.append(dict(index=index,name=item.get('name','Unknown folder'),error=str(e)))
                self.event(task,'error',str(e),folder=item.get('name','Unknown folder'))
        self.task_update(task,'failed' if errors else 'completed',finished=utcnow(),changed=len(result),errors=errors)
        return dict(task_id=task,changed=result,errors=errors)
