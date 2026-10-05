# Mover

A self-hosted dashboard for moving folders between physical storage locations, with verified copies, batch reviews, independent schedules, and persistent task history.

Built with Python's standard library, SQLite, and plain HTML/CSS/JavaScript. No runtime packages or frontend build step.

## Features

- Arbitrary location names and any number of configured media roots.
- Categories discovered from immediate, non-hidden directory names.
- Manual moves between locations or categories, including same-location category moves.
- Batch review and atomic queueing of up to 100 selected folders.
- Directory merging when relative file paths do not overlap; existing files are never overwritten.
- Folder/category locks and explicit manual overrides.
- User-assigned Cache, Cold storage, and Unassigned roles.
- Named schedules with independent routes, timezone, categories, age, capacity margin, and run limits.
- One-use schedule reviews: Run now queues exactly the previewed folders and resolved destinations.
- SHA-256 verification, repeated mount/identity checks, and individual verified source cleanup.
- Searchable task logs, structured timelines, cancellation, and restart interruption handling.

## Local demo

Requires Linux and Python 3.12 or later.

```bash
python3 scripts/seed_demo.py
MOVER_DEMO=1 MOVER_PORT=8090 python3 -m app.server
```

Open **http://localhost:8090**. The seed script creates small text fixtures under these directories, without overwriting existing files:

```text
~/Desktop/mover-test-ssd1/data/Media
~/Desktop/mover-test-drive1/data/Media
~/Desktop/mover-test-drive2/data/Media
```

Demo mode binds to loopback by default, permits ordinary directories, and needs no token. Keep it local. All demo locations share the host filesystem, so their capacity numbers match.

## Production with Docker Compose

Use Docker Compose supporting `bind.create_host_path: false`.

```bash
cp .env.example .env
mkdir -p state
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Edit `.env`: paste the generated token, set the physical media paths, and set `MOVER_UID`/`MOVER_GID` to the media owner's IDs. The service needs write/delete permissions on media and write access to `state`.

```bash
docker compose up -d --build
```

Open **http://localhost:8080** and enter the token. The browser stores it in session storage for that tab. The host port is loopback-only; use an authenticated HTTPS proxy or SSH tunnel for remote access.

The sample Compose configuration mounts two physical media directories as `Incoming` and `Library`. These are labels; both start Unassigned. Assign roles in Settings after startup. The container runs non-root with a read-only root filesystem and dropped capabilities.

### Configure your storage

Each configured path is the directory **containing categories**, not the parent disk root:

```text
/mnt/drive6/data/Media/    ← bind-mount this as a location
├── Anime/
│   └── Frieren/
├── Movies/
│   └── Arrival/
└── Documentaries/
    └── Planet Earth/
```

To add locations, add a bind mount and its matching name/path to `MOVER_ROOTS` in `compose.yaml`:

```yaml
environment:
  MOVER_ROOTS: '{"Incoming":"/media/incoming","Library":"/media/library","Archive":"/media/archive"}'
```

For each volume, use an existing physical host media directory, a matching container target, and `bind.create_host_path: false`. **Never configure the mergerfs union as a location.**

Every production media root must itself be a mountpoint. Docker bind mounts satisfy this requirement, but cannot prove the correct physical disk is mounted on the host. Verify the host mounts before startup. For direct host execution, bind-mount a media subdirectory if it is not itself a mountpoint.

New category directories appear on refresh. A missing destination category is created when a confirmed move executes; review identifies it beforehand. Hidden directories and symlink categories are excluded from discovery. Loose files directly under a media root or category are not move units.

### Storage roles

- **Cold storage:** eligible for automatic destination selection, which chooses the other cold location with the most free space that can fit the folder plus margin.
- **Cache:** a label for your incoming storage; schedules still require an explicit source.
- **Unassigned:** available for explicit manual and scheduled routes, excluded from automatic cold-storage selection.

There is no role inference from names, and no fallback from automatic cold storage to Cache or Unassigned.

## Moves and schedules

Select physical folder rows, choose the destination location/category, review, and confirm. A folder split across locations remains multiple physical rows. Category changes affect only selected folders; review lists parts remaining in the old category.

`.mvlock` locks a folder or an entire category. Scheduled moves always respect locks. A manual override applies only to that job; it does not remove the lock.

Schedules support daily, selected weekdays, or monthly days 1–28, with a local start time and IANA timezone. Eligibility uses the newest regular-file modification time. Empty folders are skipped by schedules but can move manually. Existing schedules retain their explicit category selections when categories change.

Run now uses a five-minute, one-use preview token. Confirmation rechecks reviewed manifests, collisions, and aggregate space, and queues exactly that selection. New eligible folders wait for another run. Changes to schedule settings or location roles require another preview. Eligible folders with successful reviews can run while preview errors remain recorded in task history.

One worker processes moves sequentially. Disabling or deleting a schedule does not cancel existing jobs. Missed timer runs are skipped; a repeated DST clock time fires at most once per local day. Jobs never resume automatically after restart.

**Pause writers and disable the old mover before moving folders.** Failures may leave partial moves and duplicate source/destination copies. Inspect both locations before retrying. See [Safety and recovery](docs/safety.md) for exact behavior.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MOVER_ROOTS` | unset | JSON map of location names to absolute category-containing paths; overrides legacy defaults |
| `MOVER_STATE` | `.state` locally; `/state` in image | SQLite settings/history and instance lock |
| `MOVER_TOKEN` | required in production | Random token, at least 24 characters |
| `MOVER_BIND` | `127.0.0.1` locally; `0.0.0.0` in image | HTTP listen address |
| `MOVER_PORT` | `8080` | HTTP port |
| `MOVER_DEMO` | `0` | `1` allows ordinary test directories and tokenless access |
| `MOVER_SSD1` | `/mnt/ssd1/data/Media` | Legacy default location path |
| `MOVER_DRIVE1` | `/mnt/drive1/data/Media` | Legacy default location path |
| `MOVER_DRIVE2` | `/mnt/drive2/data/Media` | Legacy default location path |

`MOVER_ROOTS` supports one or more non-overlapping locations. The name `auto` is reserved. A single location supports category moves; schedules require different source and destination locations. Root changes require a restart.

For direct host execution:

```bash
export MOVER_ROOTS='{"Incoming":"/media/incoming","Archive":"/media/archive"}'
export MOVER_TOKEN='your-random-token-at-least-24-characters'
python3 -m app.server
```

The image's health check uses port 8080; keep that container port when using the supplied Dockerfile.

## Development and tests

```bash
python3 -m unittest discover -s tests -v
```

145 backend tests passed at the last verification, along with real-browser and production-container checks. See [Verification](docs/verification.md) for commands, adversarial checks, and coverage limits.

```text
app/                Backend and static frontend
scripts/seed_demo.py Additive demo fixtures
tests/             Backend, browser, and container tests
docs/              Safety and verification notes
compose.yaml        Example production deployment
.env.example        Host paths, UID/GID, and token setup
```

Generated state, secrets, screenshots, container archives, and Python caches are excluded from Git. To export a locally built image:

```bash
docker image save mover:local -o artifacts/mover-image.tar
```

Create `artifacts` first if needed. On the target server, load the archive, configure `.env` and mounts, then run `docker compose up -d --no-build`.
