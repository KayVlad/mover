"""Real Chromium workflows with isolated temporary drives."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tempfile
import threading
from http.server import ThreadingHTTPServer
from app.server import Service, Handler
from playwright.sync_api import sync_playwright

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);roots={d:root/d for d in ('ssd1','drive1','drive2')}
    for r in roots.values():r.mkdir()
    service=Service(roots,root/'state',False,start_worker=False)
    service.save_settings({**service.settings(),'margin_gb':0})
    def put(d,cat,name,rel,data=b'test'):
        p=service.engine.folder(d,cat,name)/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p
    put('ssd1','Anime','Frieren','Season2/e2');(service.engine.folder('ssd1','Anime','Frieren')/'Season1').mkdir()
    put('drive1','Anime','Frieren','Season1/e1',b'archive')
    put('ssd1','Anime','Collision','e1');put('drive1','Anime','Collision','e1',b'other')
    put('ssd1','Movies','Locked','movie');put('ssd1','Movies','Locked','.mvlock',b'')
    put('ssd1','Movies','<img src=x onerror=alert(1)>','movie')
    put('ssd1','Movies','Second','movie')
    (roots['ssd1']/'Series').mkdir()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.service=service;server.token='browser-test-token'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':1050})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_selector('#auth-dialog[open]');page.fill('#auth-token','browser-test-token');page.click('#auth-form button')
            page.wait_for_selector('#folders tr')
            assert page.locator('#folders tr').count()==7
            custom=roots['drive2']/'Documentaries & Music'
            custom.mkdir()
            page.click('#refresh')
            page.wait_for_function("() => [...document.querySelector('#filter-category').options].some(o => o.value === 'Documentaries & Music')")
            assert page.locator('#move-category option').filter(has_text='Documentaries & Music').count()==1
            custom.rmdir()
            page.click('#refresh')
            page.wait_for_function("() => ![...document.querySelector('#filter-category').options].some(o => o.value === 'Documentaries & Music')")
            assert page.locator('#folders img').count()==0
            assert page.locator('#folders button').count()==0
            assert page.locator('#move-selected').is_disabled()
            def select(name,drive='ssd1'):
                page.select_option('#filter-drive',drive);page.fill('#search',name)
                page.locator('#folders [data-select]').check()
            select('Collision')
            page.click('#lock-selected')
            page.wait_for_function("() => document.querySelector('#folders').textContent.includes('Locked')")
            assert (service.engine.folder('ssd1','Anime','Collision')/'.mvlock').exists()
            page.click('#unlock-selected')
            page.wait_for_function("() => document.querySelector('#folders').textContent.includes('Ready')")
            assert not (service.engine.folder('ssd1','Anime','Collision')/'.mvlock').exists()
            parent=service.engine.folder('ssd1','Anime')/'.mvlock';parent.touch();page.click('#refresh')
            page.wait_for_function("() => document.querySelector('#folders').textContent.includes('Category lock')")
            page.click('#unlock-selected')
            page.wait_for_function("() => document.querySelector('#selection-errors').textContent.includes('parent category')")
            assert parent.exists();parent.unlink();page.click('#refresh')
            page.click('#move-selected');page.select_option('#move-target','drive1');page.click('#review-move')
            page.wait_for_function("() => document.querySelector('#move-error').textContent.includes('Existing destination')")
            assert page.locator('#confirm-move').is_hidden();assert not service.jobs()
            page.click('#close-move');page.click('#clear-selection')
            select('Frieren');select('Second')
            assert '2 selected' in page.locator('#selection-count').inner_text()
            page.click('#move-selected');page.select_option('#move-target','drive1');page.click('#review-move')
            page.wait_for_selector('#confirm-move:visible')
            assert page.locator('#review-result .preview-item').count()==2
            assert 'Directory merge' in page.locator('#review-result').inner_text()
            page.click('#confirm-move');page.wait_for_selector('#logs-view:visible')
            assert len(service.jobs())==2
            service.process_one();service.process_one();page.click('#refresh')
            assert (service.engine.folder('drive1','Anime','Frieren')/'Season1/e1').read_bytes()==b'archive'
            assert (service.engine.folder('drive1','Anime','Frieren')/'Season2/e2').read_bytes()==b'test'
            page.locator('#jobs .job').filter(has_text='Batch move · 2 folders').get_by_role('button',name='View details').click()
            page.wait_for_selector('#task-children button');assert page.locator('#task-children button').count()==2
            page.locator('#task-children button').filter(has_text='Anime/Frieren').click()
            page.wait_for_function("() => document.querySelector('#task-events').textContent.includes('Source file removed')")
            assert 'File content verified' in page.locator('#task-events').inner_text()
            assert page.locator('#task-dialog').get_by_role('button',name='Retry',exact=True).count()==0
            page.screenshot(path='artifacts/task-detail.png',full_page=True)
            page.click('#close-task')
            # Logs filter, detailed review errors, and pagination beyond one page.
            page.select_option('#log-status','failed')
            page.wait_for_function("() => document.querySelector('#jobs').textContent.includes('Batch review') && [...document.querySelectorAll('#jobs .state')].every(s=>s.textContent==='failed')")
            page.locator('#jobs .job').filter(has_text='Batch review').get_by_role('button',name='View details').first.click()
            try:
                page.wait_for_function("() => document.querySelector('#task-events').textContent.includes('Existing destination')",timeout=10000)
            except Exception:
                print('DETAIL DEBUG',page.locator('#task-title').inner_text(),page.locator('#task-meta').inner_text(),page.locator('#task-events').inner_text(),errors)
                raise
            page.click('#close-task');page.select_option('#log-status','')
            for i in range(55):service.create_task('review',f'Page task {i}',{},status='completed')
            page.fill('#log-search','Page task')
            page.wait_for_function("() => document.querySelectorAll('#jobs .job').length===50")
            page.click('#more-logs');page.wait_for_function("() => document.querySelectorAll('#jobs .job').length===55")
            assert page.locator('#more-logs').is_hidden()
            page.fill('#log-search','')
            # Global roles live separately from each independent schedule.
            page.click('[data-view="settings"]')
            page.select_option('[data-role="drive1"]','cache');page.select_option('[data-role="ssd1"]','cold')
            page.click('#settings-form button[type="submit"]')
            page.wait_for_function("() => document.querySelector('#notice').textContent.includes('saved')")
            assert service.global_settings()['location_roles']['ssd1']=='cold'
            page.click('[data-view="schedules"]');page.click('#new-schedule')
            page.fill('#schedule-form [name="name"]','Cold transfer')
            page.select_option('#schedule-form [name="source"]','drive1');page.select_option('#schedule-form [name="target"]','auto')
            page.select_option('#schedule-form [name="frequency"]','weekdays');page.fill('#schedule-form [name="max_folders"]','2')
            page.click('#schedule-form button[type="submit"]');page.wait_for_selector('#schedule-dialog:not([open])',state='attached')
            first=next(s for s in service.schedules() if s['name']=='Cold transfer')
            page.click('#new-schedule');page.fill('#schedule-form [name="name"]','Second route')
            page.select_option('#schedule-form [name="source"]','ssd1');page.select_option('#schedule-form [name="target"]','drive2')
            page.click('#schedule-form button[type="submit"]');page.wait_for_selector('#schedule-dialog:not([open])',state='attached')
            second=next(s for s in service.schedules() if s['name']=='Second route')
            page.check(f'[data-schedule-toggle="{first["id"]}"]')
            page.wait_for_function("() => document.querySelector('#schedule-summary').textContent.includes('1 of')")
            page.check(f'[data-schedule-toggle="{second["id"]}"]')
            page.wait_for_function("() => document.querySelector('#schedule-summary').textContent.includes('2 of')")
            page.uncheck(f'[data-schedule-toggle="{first["id"]}"]')
            page.wait_for_function("() => document.querySelector('#schedule-summary').textContent.includes('1 of')")
            assert not service.schedule(first['id'])['enabled'];assert service.schedule(second['id'])['enabled']
            page.click(f'[data-edit="{first["id"]}"]');page.select_option('#schedule-form [name="frequency"]','daily')
            page.click('#schedule-form button[type="submit"]');page.wait_for_selector('#schedule-dialog:not([open])',state='attached')
            assert service.schedule(first['id'])['frequency']=='daily'
            page.screenshot(path='artifacts/schedules-desktop.png',full_page=True)
            page.click(f'[data-preview="{first["id"]}"]');page.wait_for_selector('#policy-dialog[open]');page.click('#close-policy')
            page.click(f'[data-run="{first["id"]}"]');page.wait_for_selector('#confirm-policy:visible')
            put('drive1','Movies','Added after schedule preview','movie')
            page.click('#confirm-policy')
            page.wait_for_selector('#logs-view:visible');service.process_one();service.process_one()
            assert service.engine.folder('ssd1','Anime','Frieren').is_dir()
            assert service.engine.folder('drive1','Movies','Added after schedule preview').is_dir()
            assert not any(j['request']['name']=='Added after schedule preview' for j in service.jobs())
            page.click('[data-view="schedules"]');page.click(f'[data-delete="{second["id"]}"]');page.click('#confirm-delete')
            page.wait_for_selector('#delete-dialog:not([open])',state='attached')
            assert all(s['id']!=second['id'] for s in service.schedules())
            # Single selection still supports same-location category moves.
            page.click('[data-view="folders"]');page.click('#refresh');select('Frieren')
            page.click('#move-selected');page.select_option('#move-category','Series');page.select_option('#move-target','ssd1')
            page.click('#review-move');page.wait_for_selector('#confirm-move:visible');page.click('#confirm-move')
            service.process_one();assert service.engine.folder('ssd1','Series','Frieren').is_dir()
            # Lock override remains explicit; queued cancellation preserves source.
            page.click('[data-view="folders"]');page.click('#refresh');select('Locked')
            page.click('#move-selected');page.select_option('#move-target','drive2');page.click('#review-move')
            page.wait_for_function("() => document.querySelector('#move-error').textContent.includes('locked')")
            page.check('#override-lock');page.click('#review-move');page.wait_for_selector('#confirm-move:visible');page.click('#confirm-move')
            page.wait_for_selector('[data-cancel]');page.locator('[data-cancel]').click()
            page.wait_for_function("() => document.querySelector('#jobs').textContent.includes('cancelled')")
            assert service.engine.folder('ssd1','Movies','Locked').exists()
            page.click('[data-view="folders"]');page.fill('#search','');page.select_option('#filter-drive','')
            page.screenshot(path='artifacts/browser-test-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path='artifacts/browser-test-mobile.png',full_page=True)
            assert not errors,errors
            browser.close()
        print('PASS: selectable table and toolbar locks, batch review/conflicts/merge, detailed task logs and pagination, independent schedules CRUD/toggles/preview/run, role settings, category move, cancellation, desktop/mobile; no JavaScript errors')
    finally:
        server.shutdown();server.server_close();thread.join()
