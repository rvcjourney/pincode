# CK PIN Code Master — VPS deployment runbook

Target: **VPS + PostgreSQL 17 + Docker named volume.** The requirement driving
every decision here is that **the data survives every deploy**.

---

## Why the data survives

Deploy-wipe has two independent causes. Both are now closed.

| Layer | Failure | Fix |
|---|---|---|
| **Code** | `build_db.py` called `os.remove(db)` on every run, destroying `change_log` and every `valid_to` closure — following the old README's own instructions deleted the audit trail | Every write is an `ON CONFLICT … DO UPDATE` upsert. Nothing drops, deletes or recreates. Wiping now needs `--fresh --yes-wipe`, two explicit flags. |
| **Storage** | A SQLite file inside the app directory dies with the container | Postgres data lives in the Docker **named volume** `ck_pgdata`, under `/var/lib/docker/volumes/`, outside the repo and outside every container's lifecycle |

What does **not** touch your data: `docker compose down`, `up`, `restart`,
`build`, `pull`, rebuilding the image, upgrading the Postgres container, or
rebooting the VPS.

What **does**: `docker compose down -v`, `docker volume rm ck_pgdata`, and
`docker system prune --volumes`. Those are the only three. Do not run them.

---

## First deploy

```bash
# 1. on the VPS
git clone https://github.com/rvcjourney/pincode.git /srv/ck-pincode
cd /srv/ck-pincode
cp .env.example .env
openssl rand -base64 32          # paste into POSTGRES_PASSWORD in .env
nano .env                        # also set DATA_GOV_KEY if you have one yet

# 2. start Postgres (creates the ck_pgdata volume on first run)
docker compose up -d db
docker compose ps                # wait for "healthy"

# 3. build everything from the committed exports — no API key needed
mkdir -p raw reports
docker compose run --rm etl all
```

That's a working, verified database in one command. `all` runs bootstrap →
build → localities → export → verify, every step idempotent.

### Then upgrade to the official feed

The committed exports are a bootstrap fixture: no per-office lat/long, and
missing the offices that the old key collision dropped. Replace them with the
authoritative pull as soon as you have a free key from
[data.gov.in](https://data.gov.in/user/register):

```bash
docker compose run --rm etl fetch 5c2f62fe-5afa-4119-a499-fec9d604d5bd raw/postoffices.jsonl
docker compose run --rm etl fetch f17a1608-5f10-4610-bb50-a63c80d83974 raw/lgd_villages.jsonl
docker compose run --rm etl all      # build_db prefers postoffices.jsonl automatically
```

### Boundary polygons (optional, ~280 MB of downloads)

Gives real PIN-area shapes and centroids instead of office-average points:

```bash
curl -L -o raw/pincode_area.xlsx \
  "https://raw.githubusercontent.com/er-data-storage/postal-code-data/master/Derived%20Information/pincode_geographical_area.xlsx"
curl -L -o raw/india-pincode.geojson \
  "https://media.githubusercontent.com/media/er-data-storage/postal-code-data/master/india-pincode.geojson"
docker compose run --rm etl geo
docker compose run --rm etl build
```

`build` creates the schema if absent and upserts. It is safe to re-run at any
time, including on every deploy.

---

## Routine operations

```bash
docker compose run --rm etl all        # bootstrap + build + localities + export + verify
docker compose run --rm etl build      # re-load / upsert. Safe. Idempotent.
docker compose run --rm etl localities # rebuild the place-name -> PIN layer
docker compose run --rm etl verify     # read-only integrity check, exit 1 on failure
docker compose run --rm etl export     # regenerate flat files + QA_SUMMARY.md
docker compose run --rm etl test       # 20 regression tests
docker compose run --rm etl refresh --dry-run   # see the monthly delta first
docker compose run --rm etl refresh             # apply + write a dated report
```

### Monthly refresh (cron on the host)

```cron
# 03:00 on the 5th — a few days after the Department of Posts publishes
0 3 5 * * cd /srv/ck-pincode && docker compose run --rm etl refresh >> /var/log/ck-refresh.log 2>&1
```

`refresh` **exits 4 and applies nothing** if the snapshot would close more than
2% of offices. A truncated API pull looks exactly like a mass closure event, and
silently accepting one would wipe live serviceability. Read
`reports/change_report_<date>.md` before ever using `--force`.

---

## Backups

The volume protects against deploys. Backups protect against everything else —
a bad refresh, a dropped table, a dead VPS.

```cron
0 3 * * * cd /srv/ck-pincode && docker compose run --rm backup >> /var/log/ck-backup.log 2>&1
```

Writes a compressed `pg_dump` into the `ck_pgbackup` volume and keeps the 14
most recent. Copy them off the box — a backup on the same VPS is not a backup:

```bash
docker run --rm -v ck_pgbackup:/b -v /srv/backups:/out alpine \
  sh -c 'cp /b/$(ls -1t /b | head -1) /out/'
# then rsync/rclone /srv/backups to somewhere else entirely
```

### Restore

```bash
docker compose up -d db
docker compose run --rm --entrypoint sh backup -c \
  'pg_restore -h db -U ck -d ck_pincode --clean --if-exists /backup/ck_pincode_<stamp>.dump'
docker compose run --rm etl verify
```

### Verify a backup is restorable (do this once, now)

An untested backup is a guess. Restore into a scratch database and check:

```bash
docker compose exec db createdb -U ck restore_test
docker compose run --rm --entrypoint sh backup -c \
  'pg_restore -h db -U ck -d restore_test /backup/<file>.dump'
docker compose exec db psql -U ck -d restore_test -c 'SELECT COUNT(*) FROM post_office'
docker compose exec db dropdb -U ck restore_test
```

---

## Connecting the CK App

The DB is bound to `127.0.0.1:5432` — **not** reachable from the internet. Keep
it that way. Options, in order of preference:

1. **App on the same VPS, same compose network** — connect to host `db`, port
   `5432`. Nothing is exposed.
2. **App elsewhere** — SSH tunnel: `ssh -L 5432:localhost:5432 user@vps`, then
   `CK_DB_URL=postgresql://ck:pw@localhost:5432/ck_pincode`.
3. **Public exposure** — only behind TLS, a firewall allowlist and a
   read-only role. Create one:

```sql
CREATE ROLE ck_app LOGIN PASSWORD 'another-long-random-string';
GRANT CONNECT ON DATABASE ck_pincode TO ck_app;
GRANT USAGE ON SCHEMA public TO ck_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ck_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ck_app;
```

The app reads `v_pincode_lookup` (checkout / onboarding), `v_pincode_offices`
("select your nearest post office") and `pincode_district_map` — never
`primary_district` alone, because 1,245 PINs span more than one district.

---

## Sizing

| | |
|---|---|
| Postgres + indexes, no polygons | ~200 MB |
| With `pincode_boundary` polygons | ~450 MB |
| Each `pg_dump` (compressed) | ~60–120 MB |
| 14 retained dumps | ~1.5 GB |
| **Recommended VPS disk** | **20 GB minimum** |
| RAM | 2 GB works; 4 GB comfortable during refresh |

---

## Troubleshooting

**`POSTGRES_PASSWORD is required`** — `.env` is missing or unset. Compose reads
`.env` from the directory you run it in.

**Postgres won't start after a volume restore** — check ownership:
`docker compose run --rm --entrypoint sh db -c 'ls -la /var/lib/postgresql/data'`.
`PGDATA` is deliberately a subdirectory so a stray file in the volume root
cannot block initdb.

**`psycopg` not installed** — only needed for `postgresql://` URLs.
`pip install 'psycopg[binary]'`, or use the `etl` container which has it.

**Refresh exited 4** — the guard worked. Read the blocked report in `reports/`.
Almost always a truncated API pull, not a real mass closure. Re-run the fetch.

**Confirm the volume is real and separate:**

```bash
docker volume inspect ck_pgdata --format '{{.Mountpoint}}'
# /var/lib/docker/volumes/ck_pgdata/_data   <- outside the repo. Correct.
```
