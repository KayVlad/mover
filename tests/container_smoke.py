"""Exercise the built image in production mode against isolated bind mounts."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.request
import urllib.error

image=os.environ.get('MOVER_TEST_IMAGE','localhost/mover:latest')
with tempfile.TemporaryDirectory(prefix='mover-container-') as tmp:
    base=Path(tmp)
    roots={d:base/d for d in ('ssd1','drive1','drive2','state')}
    for p in roots.values():p.mkdir()
    src=roots['ssd1']/'Anime/Frieren'
    src.mkdir(parents=True);(src/'Season2').mkdir();(src/'Season2/e2').write_bytes(b'new season')
    (src/'Season1').mkdir()
    dst=roots['drive1']/'Anime/Frieren/Season1'
    dst.mkdir(parents=True);(dst/'e1').write_bytes(b'archive season')
    name='mover-qa-'+secrets.token_hex(4)
    token=secrets.token_urlsafe(32)
    args=['podman','run','-d','--name',name,'--userns=keep-id','--user',f'{os.getuid()}:{os.getgid()}',
          '--read-only','--tmpfs','/tmp','--cap-drop=ALL','--security-opt=no-new-privileges',
          '-p','127.0.0.1::8080','-e','MOVER_TOKEN='+token]
    for d,p in roots.items():args.extend(['-v',f'{p}:'+('/state' if d=='state' else '/mnt/'+d+'/data/Media')])
    args.append(image)
    subprocess.run(args,check=True,capture_output=True)
    try:
        port=subprocess.check_output(['podman','port',name,'8080'],text=True).strip().rsplit(':',1)[1]
        url='http://127.0.0.1:'+port
        def api(path,data=None,authenticated=True):
            request=urllib.request.Request(url+'/api/'+path,data=json.dumps(data).encode() if data is not None else None,
              headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if authenticated else {})})
            with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)
        def wait_ready():
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                try:return api('state')
                except (OSError,urllib.error.URLError):time.sleep(.2)
            raise AssertionError(subprocess.check_output(['podman','logs',name],text=True))
        state=wait_ready()
        assert not state['demo'] and not state['settings']['enabled']
        try:api('state',authenticated=False);raise AssertionError('Unauthorized API accepted')
        except urllib.error.HTTPError as e:assert e.code==401
        settings={**state['settings'],'margin_gb':0,'age_days':77,'source':'drive1','target':'auto','location_roles':{'ssd1':'cold','drive1':'cache','drive2':'unassigned'}}
        api('settings',settings)
        role_review=api('review',dict(source='drive1',category='Anime',name='Frieren',target='auto',destination_category='Series'))
        assert role_review['target']=='ssd1'
        api('lock',dict(drive='ssd1',category='Anime',name='Frieren',locked=True))
        assert (src/'.mvlock').is_file()
        api('lock',dict(drive='ssd1',category='Anime',name='Frieren',locked=False))
        assert not (src/'.mvlock').exists()
        plan=api('review',dict(source='ssd1',category='Anime',name='Frieren',target='drive1'))
        api('move',dict(review_token=plan['review_token']))
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            job=api('state')['jobs'][0]
            if job['status'] in ('completed','failed'):break
            time.sleep(.2)
        assert job['status']=='completed',job
        assert not src.exists()
        assert (dst/'e1').read_bytes()==b'archive season'
        assert (dst.parent/'Season2/e2').read_bytes()==b'new season'
        subprocess.run(['podman','restart',name],check=True,capture_output=True)
        state=wait_ready()
        assert state['settings']['age_days']==77
        assert state['settings']['source']=='drive1' and state['settings']['target']=='auto'
        assert state['settings']['location_roles']['ssd1']=='cold'
        assert state['jobs'][0]['status']=='completed'
        # Host keeps source intact when the mounted destination has a collision.
        src.mkdir();(src/'Season2').mkdir();(src/'Season2/e2').write_bytes(b'another copy')
        try:api('review',dict(source='ssd1',category='Anime',name='Frieren',target='drive1'));raise AssertionError('Collision accepted')
        except urllib.error.HTTPError as e:
            assert e.code==400 and 'Existing destination' in json.load(e)['error']
        assert (src/'Season2/e2').exists()
        print('PASS: production-mode container, non-root/read-only filesystem, mounted physical branches, auth, user-assigned roles and cold-storage selection, folder lock toggles, split merge, persisted settings/history after restart, explicit collision refusal')
    finally:
        subprocess.run(['podman','rm','-f',name],capture_output=True)
