# Safety and recovery

Mover operates on physical locations. Do not configure a mergerfs union as a source or destination. Stop other mover instances/scripts and pause applications writing to selected folders.

## Move sequence

1. Validate media roots and paths; inspect source manifests, locks, collisions, and destination capacity plus margin.
2. Acquire advisory locks at every configured media root to coordinate cooperating Mover instances.
3. Copy each source file into a temporary destination file. Check source identity and SHA-256 contents; preserve file permissions and timestamps.
4. Publish using an exclusive hard link, never replacing an existing destination entry. Sync file data and destination directories.
5. Recheck the entire source manifest, both copies, and collisions on other physical branches.
6. Remove verified source files individually. Remove directories only when empty; never recursively delete source trees.

New files are owned by the service user. New destination directories receive source directory ownership/metadata; failure to preserve required metadata stops the move. Existing destination directories retain their metadata. Use a service UID/GID compatible with your media.

Existing file collisions are errors, including byte-identical duplicates. File/directory conflicts, symlinks, hard-linked files, special files, and differing-device filesystem boundaries are refused. Checks include category and folder paths beneath each media root. A same-device bind mount is not guaranteed to be identified as a filesystem boundary by device checks.

Mount checks verify availability, mountpoint status in production, and root device/inode identity captured at startup. Roots are checked repeatedly during moves. Docker bind mounts do not prove the underlying host directory is on the intended physical disk; arrange host mount ordering and verify disks before starting Mover.

## Failure behavior

| Event | Expected behavior |
|---|---|
| Collision or capacity failure before copying | Move refused; source retained |
| Read/write/permission/verification failure | Job fails; unremoved source files retained; published destination files may remain |
| Cancellation during copying | Remaining source data retained; published files can remain |
| Cancellation or failure during cleanup | Some files already moved, others remain on the source; no rollback |
| Process crash | Published copies and incomplete temporary files may remain |
| Restart | Queued/running tasks marked interrupted; no automatic resume |
| Retry against partial results | Existing destination files cause collision errors; no automatic overwrite or deduplication |

Inspect both branches before retrying. Retain verified destination files, determine which source files remain, and resolve duplicates deliberately. Do not bulk-delete temporary `.mover-*` files without inspection. Locks are advisory and do not coordinate with other applications or the original shell mover.

An unavailable or changed configured location can block operations even if it is not the selected source/destination, because all branches participate in safety checks.

## Active writers and durability

Metadata signatures and repeated checksums detect many source changes. They do not detect every open descriptor or eliminate the final check-to-unlink race against an uncooperative writer.

A process can keep an open source descriptor after its pathname is removed. Subsequent writes go to that unlinked inode, not the destination, and can disappear when the descriptor closes. This behavior was reproduced in a test. Pause downloaders and other writers before moves.

Destination fsync operations precede source cleanup, but software cannot guarantee durability on faulty hardware or disks that fail to honor flushes. Physical disk disconnects, machine power loss, and real mergerfs integration have not been tested in this project environment. Disk I/O can hang instead of returning a prompt error.

## Access

Production requires an access token of at least 24 characters. The supplied Compose file exposes HTTP only on host loopback. Use an authenticated HTTPS reverse proxy or an SSH tunnel for remote access. Demo mode allows tokenless access and ordinary directories; keep it local.
