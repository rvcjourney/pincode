#!/usr/bin/env python3
"""
The derived `pincode` rollup, computed in ONE place and shared by build_db.py
and refresh.py.

Two bugs in the original lived here and are fixed:

  1. The rollup was aggregated from the in-memory input list rather than from
     the rows that actually landed in post_office. Offices dropped by an
     office_key collision were still counted, so n_offices over-reported by 16
     and PIN 284204 was falsely flagged as multi-district. It now aggregates
     strictly from `SELECT ... FROM post_office WHERE valid_to IS NULL`.

  2. A PIN that lost its last office was marked status='retired' but kept its
     old n_offices / n_delivery / is_deliverable values, so v_pincode_lookup
     still advertised a dead PIN as deliverable. Retirement now zeroes every
     office-derived column (geometry is deliberately left intact).
"""
from collections import defaultdict, Counter

# Office-derived columns. These are the only ones the rollup owns; geometry
# (centroid, bbox, area, has_boundary) belongs to the geo step and is never
# touched here.
DERIVED = ["n_offices", "n_delivery", "is_deliverable", "primary_office",
           "primary_district", "primary_state", "n_districts", "n_states"]

# What a PIN looks like once it has no live offices left.
EMPTY = {"n_offices": 0, "n_delivery": 0, "is_deliverable": 0, "primary_office": None,
         "primary_district": None, "primary_state": None, "n_districts": 0, "n_states": 0}

TYPE_RANK = {"HO": 0, "SO": 1}      # HO > SO > BO when picking the primary office


def aggregate(db):
    """Aggregate current post_office rows into per-PIN values."""
    agg = defaultdict(lambda: {"n": 0, "nd": 0, "d": Counter(), "s": Counter(), "off": []})
    for r in db.rows("""SELECT pincode, office_type, delivery_status, office_name,
                               district_raw, state_raw
                        FROM post_office WHERE valid_to IS NULL"""):
        x = agg[r["pincode"]]
        x["n"] += 1
        if (r["delivery_status"] or "").lower().startswith("delivery"):
            x["nd"] += 1
        if r["district_raw"]:
            x["d"][r["district_raw"]] += 1
        if r["state_raw"]:
            x["s"][r["state_raw"]] += 1
        x["off"].append((TYPE_RANK.get(r["office_type"], 2), r["office_name"] or ""))

    out = {}
    for pin, x in agg.items():
        x["off"].sort()
        out[pin] = {
            "n_offices": x["n"],
            "n_delivery": x["nd"],
            "is_deliverable": 1 if x["nd"] else 0,
            "primary_office": x["off"][0][1] if x["off"] else None,
            "primary_district": x["d"].most_common(1)[0][0] if x["d"] else None,
            "primary_state": x["s"].most_common(1)[0][0] if x["s"] else None,
            "n_districts": len(x["d"]),
            "n_states": len(x["s"]),
        }
    return out


def recompute(db, now, snapshot_id=None, changes=None, verbose=True):
    """Rebuild the pincode rollup from post_office. Idempotent.

    changes: optional list; if given, a (detected_at, snapshot_id, entity,
             entity_key, change_type, field, old, new) tuple is appended for
             every value that actually moved.
    Returns (n_active, n_retired, n_new).
    """
    agg = aggregate(db)
    old = {r["pincode"]: r for r in db.rows(
        "SELECT pincode, " + ",".join(DERIVED) + ", status FROM pincode")}

    def log(pin, kind, field, o, n):
        if changes is not None:
            changes.append((now, snapshot_id, "pincode", pin, kind, field,
                            None if o is None else str(o), None if n is None else str(n)))

    # ---- PINs that currently have offices -> active
    upserts = []
    n_new = 0
    for pin, v in agg.items():
        if pin not in old:
            n_new += 1
            log(pin, "added", None, None, "new PIN in directory")
        else:
            for f in DERIVED:
                if _norm(old[pin].get(f)) != _norm(v[f]):
                    log(pin, "modified", f, old[pin].get(f), v[f])
            if old[pin].get("status") != "active":
                log(pin, "modified", "status", old[pin].get("status"), "active")
        upserts.append((pin, pin[:1], pin[:2], pin[:3],
                        v["n_offices"], v["n_delivery"], v["is_deliverable"],
                        v["primary_office"], v["primary_district"], v["primary_state"],
                        v["n_districts"], v["n_states"],
                        1 if pin[:1] == "9" else 0, now, now, "active"))
    _upsert_pincode(db, upserts)

    # ---- PINs that no longer have any office -> retired AND zeroed (bug #3)
    gone = [p for p, r in old.items() if p not in agg
            and (r.get("status") != "retired" or _has_stale(r))]
    for pin in gone:
        if old[pin].get("status") == "active":
            log(pin, "removed", "status", "active", "retired")
        for f in DERIVED:
            if _norm(old[pin].get(f)) != _norm(EMPTY[f]):
                log(pin, "modified", f, old[pin].get(f), EMPTY[f])
    if gone:
        db.executemany(
            "UPDATE pincode SET status='retired', last_seen=?, "
            + ", ".join(f"{f}=?" for f in DERIVED) + " WHERE pincode=?",
            [(now, *[EMPTY[f] for f in DERIVED], p) for p in gone])

    n_active, n_retired = len(agg), len(old) - len(agg) + n_new
    if verbose:
        print(f"[i] rollup: {n_active:,} active PINs, {n_new:,} new, "
              f"{len(gone):,} retired/zeroed this run", flush=True)
    return n_active, max(n_retired, 0), n_new


def _has_stale(row):
    """A retired PIN still carrying office-derived values from before it died."""
    return any(_norm(row.get(f)) != _norm(EMPTY[f]) for f in DERIVED)


def _norm(v):
    """Treat 0/None/'' alike so int-vs-None from different engines never
    produces a phantom change_log row."""
    return None if v in (None, "", 0) else v


def _upsert_pincode(db, rows):
    if not rows:
        return
    db.executemany(
        """INSERT INTO pincode
             (pincode, postal_zone, sub_zone, sorting_district, n_offices, n_delivery,
              is_deliverable, primary_office, primary_district, primary_state,
              n_districts, n_states, is_army_postal, first_seen, last_seen, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (pincode) DO UPDATE SET
             n_offices        = excluded.n_offices,
             n_delivery       = excluded.n_delivery,
             is_deliverable   = excluded.is_deliverable,
             primary_office   = excluded.primary_office,
             primary_district = excluded.primary_district,
             primary_state    = excluded.primary_state,
             n_districts      = excluded.n_districts,
             n_states         = excluded.n_states,
             is_army_postal   = excluded.is_army_postal,
             last_seen        = excluded.last_seen,
             status           = excluded.status""",
        rows)
    # NB: first_seen is intentionally absent from DO UPDATE - it must never move.
