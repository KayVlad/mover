# Verification

Verified locally on 2026-10-05:

- 145 backend tests passed on Python 3.13, including 180 generated split-folder layouts.
- Chromium workflows passed: dynamic category discovery, batch review, locks, split merges, collisions, task timelines, log pagination, independent schedules, reviewed Run now selection, cancellation, and desktop/mobile layout. No JavaScript errors.
- Production container integration passed with mounted media roots, token authentication, a non-root user, a read-only container filesystem, dropped capabilities, roles, merge/collision checks, and persistence after restart.
- Real nested tmpfs mounts at category and folder level were rejected beneath bind-mounted media roots.

## Reproduce

Backend tests need no third-party dependencies:

```bash
python3 -m unittest discover -s tests -v
```

Browser tests use isolated temporary media roots:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/playwright install chromium
mkdir -p artifacts
.venv/bin/python tests/browser_smoke.py
```

Container checks require Podman:

```bash
podman build --format docker -t localhost/mover:latest .
python3 tests/container_smoke.py
python3 tests/container_boundaries_smoke.py
```

Screenshots, container exports, state databases, and Python caches are generated locally and excluded from Git.

## Adversarial checks

Fault injection is confined to temporary drives. Tests cover:

- Cancellation during copying, verification, cleanup, and directory removal.
- Root-directory replacement during copying and cleanup.
- Destination corruption during copying, verification, and cleanup.
- Destination folder replacement with a symlink; outside sentinel files remain untouched.
- Collisions appearing on a third physical branch after preflight.
- Actual process SIGKILL during copying, after copying, and during cleanup.
- Permission, disk-space, scan, read, fsync, publication, and deletion failures.
- Schedule review expiry/reuse, changed files/settings/roles/locks, pinned automatic destinations, and atomic queue rollback.

Assertions check original contents remain at the source or a published destination. Failures during cleanup can leave a partially moved folder; this is not a rollback.

A late third-branch collision was reproduced and fixed by rescanning other branches before source cleanup. Another guard was added between source hashing and destination temporary-file creation.

A real writable descriptor demonstrated the open-writer limitation: after the source is unlinked, that descriptor can receive updates that never reach the destination. This test documents the limitation; it does not claim to prevent it.

Root replacement is simulated with directory renames. SIGKILL and nested tmpfs mounts are real operations. Physical disk disconnects, machine power loss, faulty hardware, and real mergerfs FUSE behavior have not been tested here. See [Safety and recovery](safety.md).
