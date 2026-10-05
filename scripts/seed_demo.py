"""Create additive test fixtures only in the three named Desktop test drives."""
from pathlib import Path
import os
import time

roots = {d: Path.home() / 'Desktop' / ('mover-test-' + d) for d in ('ssd1', 'drive1', 'drive2')}
fixtures = [
 ('ssd1','Anime','Frieren/Season1'),
 ('ssd1','Anime','Frieren/Season2/episode-01.mkv'),
 ('drive1','Anime','Frieren/Season1/episode-01.mkv'),
 ('ssd1','Movies','Arrival/movie.mkv'),
 ('ssd1','Movies','Arrival/.mvlock'),
 ('ssd1','Series','Severance/episode-01.mkv'),
 ('ssd1','Anime','Collision/episode-01.mkv'),
 ('drive1','Anime','Collision/episode-01.mkv'),
 ('drive2','Movies','Archive/sample.mkv'),
]
for drive, cat, rel in fixtures:
    p = roots[drive] / 'data' / 'Media' / cat / rel
    if p.exists():
        continue
    if '.' not in p.name:
        p.mkdir(parents=True, exist_ok=True)
        continue
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('xb') as f:
        f.write((drive + '/' + rel + '\n').encode() * (1 if p.name=='.mvlock' else 3000))
    if cat != 'Series':
        old = time.time() - 90*86400
        os.utime(p, (old,old))
print('Demo drives:')
for path in roots.values():
    path.mkdir(parents=True,exist_ok=True)
    print(path)
