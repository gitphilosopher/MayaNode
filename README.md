# MayaNode

MayaNode is a small, self-contained persistence hub: a FastAPI + SQLite
service that stores heartbeats, an append-only event log, per-device
key/value memory, and syncs both with other clients (e.g. MayaVE) over
a simple HTTP protocol. It is infrastructure only — it has no opinion
about what a "mood" or a "note" *means*; that belongs to whatever talks
to it.

This document covers Phase 3: running MayaNode reliably, long-term, on
a Redmi Note 4 under Termux (or any similar small always-on device).

- [What's here](#whats-here)
- [Setup](#setup)
- [Running it](#running-it)
- [Configuration](#configuration)
- [Operating it long-term](#operating-it-long-term)
- [Backup and recovery](#backup-and-recovery)
- [Logging](#logging)
- [Testing](#testing)
- [API surface](#api-surface)
- [Troubleshooting](#troubleshooting)

## What's here

```
api/            FastAPI routers — one file per endpoint group
services/       Business logic, one function per operation
database/       SQLite connection handling, migrations, backup/restore
node/           App wiring: config, the app factory, the wire protocol
database/migrations/   Numbered .sql files, applied in order at startup
docs/           Protocol contract (see MayaVE integration)
test_*.py       Regression tests (stdlib unittest)
```

MayaNode and MayaVE are **separate repositories**. Nothing here imports
from or reasons about MayaVE, and this repo doesn't copy any of its
code — see `node/protocol.py`'s module docstring for how the two sides
stay compatible (a shared contract document plus contract tests, not
shared code).

## Setup

Requires Python 3.11+ (uses `X | Y` union type syntax and other modern
typing). Termux ships a recent enough Python via `pkg install python`.

```bash
# Termux packages MayaNode itself needs:
pkg install python git

git clone <this repo's URL> mayanode
cd mayanode

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

No database setup step is needed — the first run applies every
migration under `database/migrations/` automatically and creates
`database/mayanode.db` (WAL mode) if it doesn't exist yet.

## Running it

```bash
source .venv/bin/activate
python -m node
```

By default this listens on `127.0.0.1:8000` only (loopback — there is
no authentication yet, so nothing outside the device can reach it
unless you explicitly opt in via `MAYANODE_HOST`, see below). Verify
it's alive:

```bash
curl http://127.0.0.1:8000/status
curl http://127.0.0.1:8000/health
```

`/health` returns `200` when every registered check passes and `503`
the moment any one of them fails (currently: the process itself, and a
SQLite write-lock check) — point a device monitor or uptime check at
this endpoint.

Stop it with `Ctrl+C` (SIGINT) or `kill <pid>` (SIGTERM). Either way,
MayaNode finishes in-flight requests (up to `MAYANODE_GRACEFUL_TIMEOUT_S`,
default 10s), checkpoints the SQLite WAL back into the main database
file, flushes its logs, and exits — see
[Operating it long-term](#operating-it-long-term) for what that buys
you if the process later gets killed *without* a chance to do this
(e.g. the OS's low-memory killer).

## Configuration

Everything tunable lives in `node/config.py`, read once at startup from
environment variables — nothing else in the codebase touches
`os.environ` directly. Every setting has a safe default, so MayaNode
runs with zero configuration; override only what you need to.

Two ways to set them:

1. **Real environment variables** — `export MAYANODE_PORT=9000` before
   running, or prefix the command: `MAYANODE_PORT=9000 python -m node`.
2. **A `.env` file** — copy `.env.example` to `.env` and edit it. Real
   environment variables always win over `.env` if both set the same
   key, and a missing `.env` is not an error.

| Variable | Default | Meaning |
|---|---|---|
| `MAYANODE_HOST` | `127.0.0.1` | Bind address. `0.0.0.0` opens it to the LAN — only do this on a trusted network; there is no auth yet. |
| `MAYANODE_PORT` | `8000` | Bind port. |
| `MAYANODE_GRACEFUL_TIMEOUT_S` | `10` | Seconds in-flight requests get to finish after a shutdown signal before the connection is forced closed. |
| `MAYANODE_DB` | `database/mayanode.db` | Path to the SQLite file. Parent directories are created automatically if missing. |
| `MAYANODE_DB_TIMEOUT_S` | `5` | Max seconds a connection waits on a locked database before giving up (surfaces to clients as HTTP 503, see below). Also bounds `/health`. |
| `MAYANODE_DB_SYNCHRONOUS` | `NORMAL` | SQLite's `PRAGMA synchronous`: `OFF`\|`NORMAL`\|`FULL`\|`EXTRA`. `NORMAL` (SQLite's own recommendation for WAL mode) is crash-safe with far fewer `fsync()` calls than `FULL` — worth it on phone flash storage. Use `FULL` if you want maximum durability at the cost of write latency. |
| `MAYANODE_LOG_DIR` | `logs/` | Where the rotating log file is written. |
| `MAYANODE_LOG_LEVEL` | `INFO` | `DEBUG`\|`INFO`\|`WARNING`\|`ERROR`\|`CRITICAL`. |
| `MAYANODE_LOG_MAX_BYTES` | `1000000` | Rotate `logs/node.log` at this size. |
| `MAYANODE_LOG_BACKUP_COUNT` | `3` | How many rotated copies to keep (so total log disk usage is bounded at roughly `MAX_BYTES × (BACKUP_COUNT + 1)`). |
| `MAYANODE_ACCESS_LOG` | `true` | Log every request (method, path, status, latency). Turn off to save disk space. |
| `MAYANODE_BACKUP_DIR` | `database/backups/` | Where `database/backup.py` writes snapshots. |
| `MAYANODE_BACKUP_RETENTION` | `14` | How many backup files to keep before pruning the oldest. `0` keeps everything. |

If a value is malformed (e.g. `MAYANODE_PORT=not-a-number`), MayaNode
refuses to start with a clear error naming the offending variable,
rather than silently falling back to a default or failing later in a
confusing way.

## Operating it long-term

MayaNode itself has **no built-in process supervisor or scheduler** —
by design (Phase 3 deliberately doesn't add one). If the process dies
(crash, phone reboot, Termux session killed, low-memory killer), it
stays down until something restarts it. Two good options on Termux:

**Termux:Boot**, so it starts on device boot:

```bash
pkg install termux-boot
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-mayanode.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
cd ~/mayanode
source .venv/bin/activate
exec python -m node >> logs/boot.log 2>&1
EOF
chmod +x ~/.termux/boot/start-mayanode.sh
```

(Install the Termux:Boot app from the same source as Termux itself,
then open it once so Android registers the boot receiver.)

**A restart-on-crash loop**, since Termux:Boot only covers device boot,
not an in-session crash. A simple supervising loop in `tmux`/`screen` is
enough for a single-device deployment:

```bash
while true; do
  python -m node
  echo "MayaNode exited ($?), restarting in 5s..." >> logs/supervisor.log
  sleep 5
done
```

Whichever you use, remember: SQLite's WAL mode already means an
ungraceful kill mid-write loses at most the last uncheckpointed commit
— it does not corrupt the database. Combined with regular backups (see
below), a crash is an inconvenience, not a data-loss event.

**Keeping the device from killing Termux itself**: acquire a wake lock
(`termux-wake-lock`, from the `termux-api` package) if you need
MayaNode to survive Android's aggressive background-process trimming,
and exclude Termux from battery optimization in Android's app settings.

## Backup and recovery

`database/backup.py` snapshots the live database using SQLite's
built-in *online backup API* — not a raw file copy. That distinction
matters under WAL: copying the `.db` file directly can capture an
inconsistent snapshot while committed data still sits in the `-wal`
sidecar file. The backup API instead reads a transactionally consistent
copy while the live database keeps working throughout.

```bash
# Take a backup now (writes to database/backups/ by default)
python -m database.backup

# List existing backups
python -m database.backup --list

# Restore a backup over the live database (STOP THE SERVER FIRST)
python -m database.backup --restore database/backups/mayanode-20260101T000000Z.db --yes
```

Notes:

- Backups are named `mayanode-<UTC timestamp>.db`, oldest-first when
  listed. Only the most recent `MAYANODE_BACKUP_RETENTION` (default 14)
  are kept — older ones are pruned automatically after each new backup.
- `--restore` refuses to run without `--yes` (it's destructive), and
  automatically saves the *current* live database (plus any `-wal`/
  `-shm` sidecar files) to `mayanode.pre-restore-<timestamp>.db` right
  next to it before overwriting — so a mistaken restore is itself
  recoverable.
- **Always stop the MayaNode process before restoring.** Restoring
  while the server is running will fight with its own open connections.
- There is no restore-while-running story and none is planned — this
  is an offline recovery tool, not a hot failover mechanism.

**Scheduling backups**: MayaNode itself doesn't run a scheduler (Phase
3 rule — no background schedulers in the app yet). Use `cron` (via
Termux's `pkg install cronie`) or a Termux:Boot script that loops with
`sleep`:

```bash
# crontab -e  (after `pkg install cronie` and starting the crond service)
0 * * * * cd ~/mayanode && .venv/bin/python -m database.backup >> logs/backup.log 2>&1
```

**Recovering from a corrupted or lost database**: if `database/mayanode.db`
is missing entirely, `python -m node` recreates an empty one and
reapplies every migration — you lose data but the service comes back
up. To recover data instead, restore your most recent backup (above)
before starting the server.

## Logging

All logs go to `logs/node.log` (or `MAYANODE_LOG_DIR`), size-rotated at
`MAYANODE_LOG_MAX_BYTES` with `MAYANODE_LOG_BACKUP_COUNT` old copies
kept (`node.log`, `node.log.1`, `node.log.2`, ...). This captures three
sources in one place:

- MayaNode's own operational log (`mayanode`, `mayanode.db`,
  `mayanode.health`, `mayanode.migrate`, `mayanode.backup`) — startup,
  shutdown, migrations applied, health check failures, database busy/
  unavailable events.
- Every HTTP request (`uvicorn.access`) — method, path, status,
  latency. Disable with `MAYANODE_ACCESS_LOG=false` if disk space is
  tight.
- Uvicorn's own server/error log (`uvicorn.error`) — startup/shutdown
  messages, connection-level errors.

If the log directory can't be created or written to (storage
permission issues can happen on Android), MayaNode falls back to
logging to stderr rather than failing to start — a logging problem
should never be the reason the server won't run.

**What's deliberately not logged**: request/response bodies and
`payload`/`value` field contents (which may hold whatever MayaVE or
another client chose to store) are never logged wholesale — only
device IDs, event/error types, counts, and durations. If you add new
log statements, avoid logging full request bodies for the same reason.

## Testing

```bash
pip install -r requirements.txt
python -m unittest discover -p "test_*.py" -v
```

Test files:

| File | Covers |
|---|---|
| `test_node_protocol.py` | The wire protocol layer (envelope validation, versioning, ordering) and its wiring into `/sync`. |
| `test_config.py` | Environment-variable parsing, `.env` loading/precedence, invalid-value rejection. |
| `test_database_connection.py` | Connection pragmas, lock-contention → `DatabaseBusyError`, unreachable path → `DatabaseUnavailableError`, WAL checkpointing. |
| `test_migrate.py` | Migration idempotency, parent-directory auto-creation, WAL enablement, malformed migration filenames. |
| `test_backup.py` | Backup creation/content, retention pruning, restore (including the pre-restore safety copy). |
| `test_node_api.py` | Every HTTP endpoint (happy path + validation failures), plus the busy-database → clean 503 behavior. |

`test_node_protocol.py`, `test_config.py`, `test_database_connection.py`,
`test_migrate.py`, and `test_backup.py` use only the Python standard
library (plus SQLite) and will run in any Python 3.11+ environment.
`test_node_api.py` additionally needs `fastapi`/`starlette`/`httpx`
from `requirements.txt` since it drives the app over HTTP with
`fastapi.testclient.TestClient`.

Every test gets its own temporary SQLite database (and, where
relevant, its own temp log/backup directory) — nothing touches your
real `database/mayanode.db`.

## API surface

Unchanged from Phase 2 — Phase 3 adds no new endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | Liveness + version/uptime info. Always 200 if the process is up. |
| GET | `/health` | Runs registered checks (process, SQLite writability). 200 if all pass, 503 otherwise. |
| POST | `/heartbeat` | Record a device check-in. |
| GET | `/heartbeat` | List known devices and when they were last seen. |
| POST | `/events` | Append one event. |
| GET | `/events` | List events, newest first, filterable by device/type. |
| PUT | `/memory` | Create or overwrite one `(device_id, key)` value. |
| GET | `/memory` | Read one record, all records for a device, or everything. |
| GET | `/timeline` | Chronological, time-windowed browse over events. |
| POST | `/sync` | Bidirectional exchange of events/memory with a client, by cursor. |

New in Phase 3, at the HTTP layer:

- A database that's momentarily locked, or whose file can't be
  reached, now returns a clean `503 {"error": "database_busy" |
  "database_unavailable", "detail": "..."}` instead of an
  undifferentiated `500`.
- Any other unhandled server-side error returns `500
  {"error": "internal_server_error"}` with the real exception and
  traceback logged server-side (`logs/node.log`) — never leaked to the
  client.

## Troubleshooting

**"database is locked" errors under load** — increase
`MAYANODE_DB_TIMEOUT_S`. SQLite allows only one writer at a time;
occasional contention under bursty writes is expected and the built-in
retry/timeout handles it. Persistent locking usually means something
else (another process, a stuck transaction) is holding the file open —
check `logs/node.log` for `mayanode.db` warnings.

**Server won't start, "unable to open database file"** — check that
`MAYANODE_DB`'s parent directory is on writable storage.
`database/migrate.py` creates the directory itself if missing, but
can't fix read-only storage (common on some Android external-storage
mounts).

**Logs aren't appearing anywhere** — check stderr; if `MAYANODE_LOG_DIR`
couldn't be created/written, MayaNode logs to stderr instead (see
[Logging](#logging)) and also logs one `WARNING` line explaining why.

**Lost data after a crash** — restore the most recent backup (see
[Backup and recovery](#backup-and-recovery)). If you have no backups,
note that WAL mode means a crash can lose only the last few
uncommitted writes, never corrupt the file — try starting the server
normally first.

**Running behind a firewall/VPN so MayaVE on another device can reach
it** — set `MAYANODE_HOST=0.0.0.0` and open `MAYANODE_PORT` on your
network layer. There is no authentication in MayaNode yet, so only do
this on a network you trust; treat every device that can reach the
port as fully trusted.
