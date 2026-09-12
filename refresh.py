#!/usr/bin/env python3
"""
Step 4: monthly refresh + field-level diff for the CK PIN code master.

    export DATA_GOV_KEY=<your free data.gov.in key>
    python refresh.py --dry-run          # see the delta, touch nothing
    python refresh.py                    # apply + write reports/change_report_<date>.md
    python refresh.py --snapshot raw/x.jsonl   # diff an already-downloaded pull

What it does
  1. Pulls a fresh "All India Pincode Directory till last month" snapshot.
  2. Diffs it against the current (valid_to IS NULL) rows in post_office.
  3. Closes disappeared offices with valid_to instead of deleting them, so a CK
     customer address captured last year still resolves.
  4. Inserts new offices, records field-level modifications.
  5. Recomputes the pincode rollup (shared with build_db via rollup.py) and
     writes every difference to change_log.
  6. Emits reports/change_report_<date>.md.

Exit codes: 0 ok, 3 no key / fetch failed, 4 sanity guard tripped (refuses a
snapshot that would close >2% of offices - a truncated pull looks exactly like a
mass closure event and silently applying one would wipe live serviceability).
"""
import argparse, json, os, subprocess, sys, time
from collections import Counter
from datetime import datetime, timezone

import ckdb
import rollup
from build_db import (office_key, canon_state, title, OFFICE_TYPE, _lat, _lon,
                      ensure_dimensions, fill_missing_centroids, fix_coords)

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")
TODAY = NOW[:10]
RESOURCE = "5c2f62fe-5afa-4119-a499-fec9d604d5bd"
SAMPLE_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"
TRACKED = ["office_type", "delivery_status", "circle_name", "region_name",
           "division_name", "taluk", "district_raw", "state_raw", "latitude", "longitude"]


def _pages_done(path):
    """How many pages are already safely on disk, from the resume ledger."""
    try:
        with open(path + ".done") as f:
            return len(f.read().split())
    except OSError:
        return 0


def fetch_snapshot(path, key, workers, budget_minutes=45):
    """Keep fetching until every page is in, or until it is clearly stuck.

    data.gov.in throttles on both concurrency and sustained volume, so one pass
    over a 165k-row resource is regularly cut off partway. The fetcher records
    completed offsets in <out>.done and fsyncs rows *before* marking them, so
    re-running resumes exactly where it stopped - never restarting, never
    leaving a hole. This keeps resuming until the fetcher reports a complete
    pull, which is what makes a single click enough.

    Two ways it stops rather than looping forever:
      * a wall-clock budget, so a job cannot run all night; and
      * a stall detector - if three consecutive attempts add no new pages, the
        problem is not throttling and retrying will not fix it.
    Either way nothing is lost: the next run resumes from the same ledger.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.time() + budget_minutes * 60
    attempt, stalls, before = 0, 0, _pages_done(path)

    while time.time() < deadline:
        attempt += 1
        # first pass at the requested concurrency, then back off - throttling
        # responds far better to fewer requests than to more retries
        w = workers if attempt == 1 else (2 if attempt < 4 else 1)
        print(f"[i] fetching {RESOURCE} - attempt {attempt}, "
              f"{w} worker{'s' if w > 1 else ''}, {before} page(s) already done",
              flush=True)
        r = subprocess.run([sys.executable, os.path.join(HERE, "fetch_datagov.py"),
                            RESOURCE, path, "--key", key, "--workers", str(w)])

        after = _pages_done(path)
        if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            print(f"[i] complete after {attempt} attempt(s), {after} page(s)", flush=True)
            return True

        gained = after - before
        before = after
        if gained > 0:
            stalls = 0
            print(f"[i] throttled after {gained} more page(s) - resuming", flush=True)
        else:
            stalls += 1
            print(f"[!] no progress on attempt {attempt} "
                  f"({stalls}/3 before giving up)", flush=True)
            if stalls >= 3:
                print("[!] the API is not returning data - this is not throttling. "
                      "Nothing is lost; re-run later and it resumes from page "
                      f"{after}.", file=sys.stderr)
                return False

        wait = min(15 * attempt, 120)
        print(f"[i] waiting {wait}s for the rate limit to clear", flush=True)
        time.sleep(wait)

    print(f"[!] {budget_minutes}-minute budget reached with {before} page(s) done. "
          "Nothing is lost; re-run and it resumes from there.", file=sys.stderr)
    return False


def load_new(path):
    """Parse a snapshot into {office_key: row}. Duplicate keys are reported, not
    silently dropped."""
    out, dupes = {}, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            pin = str(r.get("pincode") or "").split(".")[0].zfill(6)
            name = (r.get("officename") or "").strip()
            if not name or not pin.isdigit() or len(pin) != 6:
                continue
            k = office_key(name, pin)
            if k in out:
                dupes.append(k)
                continue
            out[k] = {
                "office_name": name, "pincode": pin,
                "office_type": OFFICE_TYPE.get((r.get("officetype") or "").upper().strip()),
                "delivery_status": r.get("delivery") or r.get("deliverystatus"),
                "circle_name": r.get("circlename"), "region_name": r.get("regionname"),
                "division_name": r.get("divisionname"), "taluk": title(r.get("taluk")),
                "district_raw": title(r.get("district") or r.get("districtname")),
                "state_raw": canon_state(r.get("statename")),
                "latitude": None, "longitude": None,      # set just below
            }
            # Apply the SAME coordinate repair build_db does. Without it the
            # diff compares repaired values in the database against raw values
            # from the feed: 4,954 phantom lat/long "changes" on a snapshot with
            # no real movement, and every refresh would write the 780 swapped
            # and 1,606 out-of-India coordinates straight back in.
            out[k]["latitude"], out[k]["longitude"], _ = fix_coords(
                _lat(r.get("latitude")), _lon(r.get("longitude")))
    if dupes:
        print(f"[!] {len(dupes)} duplicate office_key(s) in snapshot, first kept", flush=True)
    return out


def diff(new, cur):
    added = [k for k in new if k not in cur]
    removed = [k for k in cur if k not in new]
    modified = []
    for k in new:
        if k not in cur:
            continue
        for fld in TRACKED:
            o, n = cur[k][fld], new[k][fld]
            if fld in ("latitude", "longitude"):
                if o is None and n is None:
                    continue
                if o is not None and n is not None and abs(o - n) < 1e-5:
                    continue
            if (o or None) != (n or None):
                modified.append((k, fld, o, n))
    return added, removed, modified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="CK_DB_URL override")
    ap.add_argument("--key", default=os.environ.get("DATA_GOV_KEY", ""))
    ap.add_argument("--workers", type=int, default=3,
                help="parallel requests; data.gov.in throttles above ~4")
    ap.add_argument("--snapshot", help="use an already-downloaded jsonl instead of fetching")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--force", action="store_true", help="skip the 2% sanity guard")
    a = ap.parse_args()

    if not a.snapshot:
        if not a.key or a.key == SAMPLE_KEY:
            print("[!] set DATA_GOV_KEY to your own free data.gov.in key.\n"
                  "    Register: https://data.gov.in/user/register -> My Account -> API key.\n"
                  "    The shared sample key is capped at 10 rows/request and is usually\n"
                  "    rate-limited to zero, so a full 165k-row pull is impossible with it.",
                  file=sys.stderr)
            sys.exit(3)
        a.snapshot = os.path.join(HERE, "raw", f"postoffices_{TODAY}.jsonl")
        if not fetch_snapshot(a.snapshot, a.key, a.workers):
            print("[!] snapshot fetch failed", file=sys.stderr)
            sys.exit(3)

    url = a.db or os.environ.get("CK_DB_URL") or ckdb.DEFAULT_URL
    print(f"[i] target: {ckdb.describe(url)}", flush=True)
    db = ckdb.connect(url)

    new = load_new(a.snapshot)
    print(f"[i] new snapshot: {len(new):,} offices", flush=True)
    cur = {r["office_key"]: r for r in db.rows(
        "SELECT * FROM post_office WHERE valid_to IS NULL")}
    print(f"[i] current in db: {len(cur):,} offices", flush=True)

    added, removed, modified = diff(new, cur)
    print(f"[i] added {len(added):,}  removed {len(removed):,}  "
          f"field changes {len(modified):,}", flush=True)

    if not a.force and cur and len(removed) > 0.02 * len(cur):
        print(f"[!] snapshot would close {len(removed):,} of {len(cur):,} offices (>2%). "
              f"Refusing. Re-run with --force only after reading the blocked report.",
              file=sys.stderr)
        write_report(added, removed, modified, new, cur, blocked=True,
                     snapshot=a.snapshot)
        sys.exit(4)

    if a.dry_run:
        write_report(added, removed, modified, new, cur, snapshot=a.snapshot)
        print("[i] dry run, nothing written")
        db.close()
        return

    snap = db.insert_returning_id(
        "INSERT INTO snapshot (source_id, fetched_at, row_count, notes) VALUES (?,?,?,?)",
        ("datagov_directory", NOW, len(new), os.path.basename(a.snapshot)), "snapshot_id")

    # New states/districts must exist before any office can reference them.
    dmap = ensure_dimensions(db, list(new.values()))

    chg = []
    for k in removed:
        db.execute("UPDATE post_office SET valid_to=? WHERE office_key=? AND valid_to IS NULL",
                   (TODAY, k))
        chg.append((NOW, snap, "post_office", k, "removed", None,
                    cur[k]["office_name"], None))

    for k, fld, o, n in modified:
        db.execute(f"UPDATE post_office SET {fld}=? WHERE office_key=? AND valid_to IS NULL",
                   (n, k))
        chg.append((NOW, snap, "post_office", k, "modified", fld,
                    None if o is None else str(o), None if n is None else str(n)))
    # a changed district/state name also moves the office's district_id
    for k in {k for k, fld, _, _ in modified if fld in ("district_raw", "state_raw")}:
        r = new[k]
        db.execute("UPDATE post_office SET district_id=? WHERE office_key=? AND valid_to IS NULL",
                   (dmap.get((r["state_raw"], r["district_raw"])), k))

    for k in added:
        r = new[k]
        db.execute(
            """INSERT INTO post_office (office_key, office_name, pincode, office_type,
                 delivery_status, circle_name, region_name, division_name, taluk,
                 district_id, district_raw, state_raw, latitude, longitude,
                 source_id, snapshot_id, valid_from)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (k, r["office_name"], r["pincode"], r["office_type"], r["delivery_status"],
             r["circle_name"], r["region_name"], r["division_name"], r["taluk"],
             dmap.get((r["state_raw"], r["district_raw"])), r["district_raw"], r["state_raw"],
             r["latitude"], r["longitude"], "datagov_directory", snap, TODAY))
        chg.append((NOW, snap, "post_office", k, "added", None, None, r["office_name"]))

    # shared with build_db: retiring a PIN zeroes its office-derived columns
    rollup.recompute(db, NOW, snapshot_id=snap, changes=chg)
    # a PIN first seen in this snapshot has no polygon yet; fall back to the mean
    # office coordinate so radius serviceability works for it immediately
    fill_missing_centroids(db)

    db.executemany(
        """INSERT INTO change_log (detected_at, snapshot_id, entity, entity_key,
             change_type, field, old_value, new_value) VALUES (?,?,?,?,?,?,?,?)""", chg)
    db.commit()
    print(f"[i] change_log rows written: {len(chg):,}", flush=True)
    write_report(added, removed, modified, new, cur, db=db, snapshot=a.snapshot)
    db.close()


def write_report(added, removed, modified, new, cur, db=None, blocked=False,
                 snapshot=None):
    d = os.path.join(HERE, "reports")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"change_report_{TODAY}.md")
    L = [f"# CK PIN code master - change report {TODAY}", ""]
    if blocked:
        L += ["> **BLOCKED** - sanity guard tripped, nothing was applied.", ""]
    L += ["## Snapshot", "",
          f"- Source: data.gov.in resource `{RESOURCE}` (Department of Posts)",
          f"- Offices in new snapshot: **{len(new):,}**",
          f"- Offices currently in DB: **{len(cur):,}**", "",
          "## Deltas", "", "| Change | Count |", "|---|---|",
          f"| Offices added | {len(added):,} |",
          f"| Offices closed (not deleted) | {len(removed):,} |",
          f"| Field-level modifications | {len(modified):,} |", ""]
    if modified:
        c = Counter(f for _, f, _, _ in modified)
        L += ["### Modifications by field", "", "| Field | Count |", "|---|---|"]
        L += [f"| `{f}` | {n:,} |" for f, n in c.most_common()]
        L.append("")
    if added:
        L += ["### Sample new offices", ""]
        L += [f"- `{new[k]['pincode']}` {new[k]['office_name']} "
              f"({new[k]['district_raw']}, {new[k]['state_raw']})" for k in added[:25]]
        L.append("")
    if removed:
        L += ["### Sample closed offices", ""]
        L += [f"- `{cur[k]['pincode']}` {cur[k]['office_name']} "
              f"({cur[k]['district_raw']})" for k in removed[:25]]
        L.append("")
    if db is not None:
        n_off = db.q("SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL")
        n_act = db.q("SELECT COUNT(*) FROM pincode WHERE status = 'active'")
        n_del = db.q("SELECT COUNT(*) FROM pincode WHERE is_deliverable = 1")
        n_ret = db.q("SELECT COUNT(*) FROM pincode WHERE status = 'retired'")
        n_msd = db.q("SELECT COUNT(*) FROM pincode WHERE n_districts > 1")
        n_log = db.q("SELECT COUNT(*) FROM change_log")
        L += ["## Post-refresh state", "", "| Metric | Value |", "|---|---|",
              f"| Current post offices | {n_off:,} |",
              f"| Active PIN codes | {n_act:,} |",
              f"| Deliverable PIN codes | {n_del:,} |",
              f"| Retired PIN codes | {n_ret:,} |",
              f"| PINs straddling >1 district | {n_msd:,} |",
              f"| change_log rows (all time) | {n_log:,} |", ""]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[i] report -> {p}", flush=True)

    # Machine-readable twin of the report. The console reads this to show what
    # WOULD change before anyone confirms an apply, and it records the snapshot
    # path so the apply reuses the downloaded file instead of pulling 165,595
    # rows a second time.
    summary = {
        "generated_at": NOW,
        "blocked": bool(blocked),
        "applied": db is not None,
        "snapshot": os.path.abspath(snapshot) if snapshot else None,
        "snapshot_offices": len(new),
        "db_offices": len(cur),
        "added": len(added),
        "removed": len(removed),
        "modified": len(modified),
        "by_field": dict(Counter(f for _, f, _, _ in modified).most_common()),
        "sample_added": [{"pincode": new[k]["pincode"], "name": new[k]["office_name"],
                          "district": new[k]["district_raw"]} for k in added[:10]],
        "sample_removed": [{"pincode": cur[k]["pincode"], "name": cur[k]["office_name"],
                            "district": cur[k]["district_raw"]} for k in removed[:10]],
        "report": os.path.basename(p),
    }
    with open(os.path.join(d, "last_refresh.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)


if __name__ == "__main__":
    main()
