#!/usr/bin/env python3
"""
HTTP interface for the CK PIN Code Master: a browsable UI plus the JSON API the
CK App calls.

    pip install fastapi uvicorn
    python serve.py                       # http://localhost:8000
    docker compose up -d web              # on the VPS

Reads whatever CK_DB_URL points at, so it serves live data - not the frozen
snapshot the static explorer page embeds. This is also the only place downloads
work: the published artifact runs in a sandbox that blocks them.

READ-ONLY. Every statement here is a SELECT; nothing mutates the master.
"""
import csv, hmac, io, itertools, json, os, subprocess, sys, threading, time
from datetime import datetime, timezone

try:
    from fastapi import FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
except ImportError:
    sys.exit("[!] pip install fastapi uvicorn")

import ckdb
import fuzzy

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "web", "ui.html")
MAX_PAGE = 500
# Upper bound for the wide-area pivot. The widest PIN covers ~1,016 areas;
# Excel tops out at 16,384 columns, so this is headroom, not a truncation.
AREA_COL_LIMIT = 2000

# Pipeline tasks runnable from the UI. An allowlist, never a free-form command:
# the value is the exact argv, so nothing a caller sends can reach a shell.
#   "writes" marks a task that mutates the master - the UI confirms those.
#   rank: 1 = the one action most people want, 2 = sensible companions,
#         3 = maintenance, hidden behind "More".
TASKS = {
    "refresh": {
        "argv": ["refresh.py"], "writes": True, "rank": 1,
        "label": "Fetch latest data",
        "desc": "Downloads the newest government snapshot, records every change "
                "in the log, and updates the master. Takes a few minutes. Nothing "
                "is deleted - closed offices are kept with a closing date."},
    "refresh_apply": {
        "argv": ["refresh.py"], "writes": True, "rank": 0, "hidden": True,
        "label": "Apply the previewed changes",
        "desc": "Applies the exact snapshot the preview examined - no second "
                "download, so what you approved is what gets written."},
    "refresh_dry": {
        "argv": ["refresh.py", "--dry-run"], "writes": False, "rank": 2,
        "label": "Preview changes first",
        "desc": "Same download and comparison, but writes nothing. Shows what "
                "would be added, closed or altered."},
    "verify": {
        "argv": ["tools/verify.py"], "writes": False, "rank": 2,
        "label": "Check data health",
        "desc": "Runs every integrity check and reports anything inconsistent."},
    "export": {
        "argv": ["export.py"], "writes": False, "rank": 3,
        "label": "Rebuild download files",
        "desc": "Regenerates the CSVs on the Downloads tab from the current data."},
    "localities": {
        "argv": ["build_localities.py"], "writes": True, "rank": 3,
        "label": "Rebuild area names",
        "desc": "Re-derives the area-to-PIN layer. Only needed after a manual "
                "data load."},
    "build": {
        "argv": ["build_db.py"], "writes": True, "rank": 3,
        "label": "Reload from downloaded files",
        "desc": "Loads whatever is in raw/ into the master. Use this after a "
                "manual download, not for routine updates."},
}

ADMIN_TOKEN = os.environ.get("CK_ADMIN_TOKEN", "").strip()

app = FastAPI(title="CK PIN Code Master", docs_url="/api/docs", redoc_url=None)


def db():
    """One connection per request. Cheap at this scale and avoids sharing a
    cursor across FastAPI's threadpool workers."""
    return ckdb.connect()


def page(limit, offset):
    return min(max(int(limit), 1), MAX_PAGE), max(int(offset), 0)


# --------------------------------------------------------------------- UI
@app.get("/", response_class=HTMLResponse)
def ui():
    if not os.path.exists(UI):
        return HTMLResponse("<h1>web/ui.html is missing</h1>", status_code=500)
    with open(UI, encoding="utf-8") as f:
        # no-store: the page and the API evolve together, and a cached page
        # talking to a newer server sends parameters the new code never emits
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})


@app.get("/form", response_class=HTMLResponse)
def address_form():
    """Reference implementation of the onboarding address step, live against the
    master. Shows the one change that matters: a PIN maps to several areas, so
    locality is a choice, and for a PIN that straddles districts, so is district."""
    p = os.path.join(HERE, "web", "form.html")
    if not os.path.exists(p):
        return HTMLResponse("<h1>web/form.html is missing</h1>", status_code=500)
    with open(p, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/health")
def health():
    try:
        d = db()
        n = d.q("SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL")
        d.close()
        return {"status": "ok", "offices": n}
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


# ------------------------------------------------------------------ stats
@app.get("/api/stats")
def stats():
    d = db()
    try:
        out = {
            "pincodes": d.q("SELECT COUNT(*) FROM pincode"),
            "active": d.q("SELECT COUNT(*) FROM pincode WHERE status='active'"),
            "deliverable": d.q("SELECT COUNT(*) FROM pincode WHERE is_deliverable=1"),
            "retired": d.q("SELECT COUNT(*) FROM pincode WHERE status='retired'"),
            "offices": d.q("SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL"),
            "offices_closed": d.q("SELECT COUNT(*) FROM post_office WHERE valid_to IS NOT NULL"),
            "multi_district": d.q("SELECT COUNT(*) FROM pincode WHERE n_districts>1"),
            "multi_state": d.q("SELECT COUNT(*) FROM pincode WHERE n_states>1"),
            "with_polygon": d.q("SELECT COUNT(*) FROM pincode WHERE has_boundary=1"),
            "localities": d.q("SELECT COUNT(*) FROM locality"),
            "states": d.q("SELECT COUNT(*) FROM state"),
            "districts": d.q("SELECT COUNT(*) FROM district"),
            "changes": d.q("SELECT COUNT(*) FROM change_log"),
        }
        out["no_centroid"] = d.q("SELECT COUNT(*) FROM pincode "
                                 "WHERE status='active' AND centroid_lat IS NULL")
        return out
    finally:
        d.close()


@app.get("/api/states")
def states():
    d = db()
    try:
        return d.rows("SELECT state_code, state_name, state_type FROM state "
                      "ORDER BY state_name")
    finally:
        d.close()


@app.get("/api/districts")
def districts(state: str = ""):
    d = db()
    try:
        if state:
            return d.rows("""SELECT d.district_name, s.state_name FROM district d
                             JOIN state s ON s.state_code=d.state_code
                             WHERE s.state_name=? ORDER BY d.district_name""", (state,))
        return d.rows("""SELECT d.district_name, s.state_name FROM district d
                         JOIN state s ON s.state_code=d.state_code
                         ORDER BY s.state_name, d.district_name""")
    finally:
        d.close()


# --------------------------------------------------------------- pincodes
@app.get("/api/pincodes")
def pincodes(q: str = "", state: str = "", district: str = "",
             multi: bool = False, deliverable: bool = False, active: bool = True,
             limit: int = Query(50), offset: int = Query(0)):
    limit, offset = page(limit, offset)
    where, args = [], []
    if active:
        where.append("status = 'active'")
    if multi:
        where.append("n_districts > 1")
    if deliverable:
        where.append("is_deliverable = 1")
    if state:
        where.append("primary_state = ?"); args.append(state)
    if district:
        where.append("primary_district = ?"); args.append(district)
    if q:
        # one box, four things: PIN prefix, office name, district, or any area
        # name the PIN covers. The last is what lets someone type "Viman Nagar"
        # instead of knowing 411014, and is why there is no separate tab for it.
        where.append("""(pincode LIKE ? OR LOWER(primary_office) LIKE ?
                         OR LOWER(primary_district) LIKE ?
                         OR pincode IN (SELECT lp.pincode FROM locality_pincode lp
                                        JOIN locality l ON l.locality_id = lp.locality_id
                                        WHERE lp.valid_to IS NULL
                                          AND LOWER(l.locality_name) LIKE ?))""")
        args += [q + "%", "%" + q.lower() + "%", "%" + q.lower() + "%", q.lower() + "%"]
    sql = " WHERE " + " AND ".join(where) if where else ""
    d = db()
    try:
        total = d.q("SELECT COUNT(*) FROM pincode" + sql, args)
        rows = d.rows(
            """SELECT pincode, primary_office, primary_district, primary_state,
                      n_offices, n_delivery, is_deliverable, n_districts, n_states,
                      centroid_lat, centroid_lon, area_sqkm, has_boundary, status
               FROM pincode""" + sql + " ORDER BY pincode LIMIT ? OFFSET ?",
            args + [limit, offset])

        # Nothing matched literally: retry through the spelling-tolerant keys, so
        # "Mohammadwadi" still finds 411060 even though the data says
        # "Mohamadwadi". Only on an empty result, so an exact search is never
        # diluted by fuzzy noise.
        if not rows and q and offset == 0:
            fz = d.rows(
                """SELECT DISTINCT lp.pincode FROM locality l
                   JOIN locality_pincode lp ON lp.locality_id = l.locality_id
                                           AND lp.valid_to IS NULL
                   WHERE l.fold_key LIKE ? OR l.skel_key = ? LIMIT 200""",
                (fuzzy.fold(q) + "%", fuzzy.skeleton(q)))
            pins = [r["pincode"] for r in fz]
            if pins:
                ph = ",".join("?" for _ in pins)
                rows = d.rows(
                    f"""SELECT pincode, primary_office, primary_district, primary_state,
                               n_offices, n_delivery, is_deliverable, n_districts,
                               n_states, centroid_lat, centroid_lon, area_sqkm,
                               has_boundary, status
                        FROM pincode WHERE pincode IN ({ph})
                        ORDER BY pincode LIMIT ?""", pins + [limit])
                total = len(pins)

        # Attach the areas each PIN covers. One extra query for the whole page,
        # not one per row - this is the column that makes "a PIN is not one
        # area" visible without opening every record.
        if rows:
            pins = [r["pincode"] for r in rows]
            ph = ",".join("?" for _ in pins)
            by = {}
            # each area carries its OWN district - for a PIN that straddles
            # districts they genuinely differ, so this is more accurate than
            # repeating the PIN's primary_district against every area
            for x in d.rows(
                    f"""SELECT lp.pincode, l.locality_name,
                               dd.district_name AS district, ss.state_name AS state
                        FROM locality_pincode lp
                        JOIN locality l ON l.locality_id = lp.locality_id
                        LEFT JOIN district dd ON dd.district_id = l.district_id
                        LEFT JOIN state ss ON ss.state_code = dd.state_code
                        WHERE lp.valid_to IS NULL AND lp.pincode IN ({ph})
                        ORDER BY lp.pincode, l.locality_name""", pins):
                by.setdefault(x["pincode"], []).append(
                    {"name": x["locality_name"], "district": x["district"],
                     "state": x["state"]})
            for r in rows:
                items = by.get(r["pincode"], [])
                for it in items:            # fall back to the PIN's own values
                    it["district"] = it["district"] or r["primary_district"]
                    it["state"] = it["state"] or r["primary_state"]
                    it["full"] = ", ".join(
                        x for x in (it["name"], it["district"], it["state"]) if x)
                r["localities"] = items
                r["n_localities"] = len(items)
        return {"total": total, "limit": limit, "offset": offset, "rows": rows}
    finally:
        d.close()


@app.get("/api/pincode/{pin}")
def pincode(pin: str):
    if not (pin.isdigit() and len(pin) == 6):
        raise HTTPException(400, "pincode must be 6 digits")
    d = db()
    try:
        rows = d.rows("SELECT * FROM pincode WHERE pincode = ?", (pin,))
        if not rows:
            raise HTTPException(404, f"PIN {pin} not found")
        rec = rows[0]
        rec["offices"] = d.rows(
            """SELECT office_name, office_type, delivery_status, taluk,
                      district_raw AS district, state_raw AS state,
                      circle_name, division_name, latitude, longitude
               FROM post_office WHERE pincode = ? AND valid_to IS NULL
               ORDER BY office_type, office_name""", (pin,))
        rec["districts_spanned"] = d.rows(
            """SELECT DISTINCT district_raw AS district, state_raw AS state
               FROM post_office WHERE pincode = ? AND valid_to IS NULL
                 AND district_raw IS NOT NULL ORDER BY district_raw""", (pin,))
        rec["localities"] = d.rows(
            """SELECT locality_name, locality_type, source_id
               FROM v_pincode_localities WHERE pincode = ?
               ORDER BY locality_name LIMIT 200""", (pin,))
        rec["closed_offices"] = d.rows(
            """SELECT office_name, office_type, valid_from, valid_to
               FROM post_office WHERE pincode = ? AND valid_to IS NOT NULL
               ORDER BY valid_to DESC LIMIT 50""", (pin,))
        # the PIN's own digits carry meaning; expose it rather than making
        # every client re-derive it
        rec["structure"] = {"zone": pin[0], "sub_zone": pin[:2],
                            "sorting_district": pin[:3], "office": pin[3:]}
        return rec
    finally:
        d.close()


# --------------------------------------------------------- full addresses
def _strip_office_suffix(name):
    import re
    return re.sub(r"\s*\b(B\.?O|S\.?O|H\.?O|G\.?P\.?O)\.?$", "",
                  re.sub(r"\s*\([^)]*\)\s*$", "", (name or "").strip())).strip()


def format_address(locality, taluk, district, state, pincode, line1=None):
    """Assemble an Indian postal address, dropping components that repeat.

    Taluk is frequently identical to the district (Pune / Pune) or to the
    locality itself; printing it twice reads as a data error to a candidate,
    so equal neighbours collapse.
    """
    parts, seen = [], set()
    for p in (line1, locality, taluk, district, state):
        if not p:
            continue
        k = p.strip().lower()
        if k and k not in seen:
            seen.add(k)
            parts.append(p.strip())
    return ", ".join(parts) + (" - " + pincode if pincode else "")


@app.get("/api/address/{pin}")
def address(pin: str):
    """Every full address this PIN covers - one per locality.

    A PIN is not one address. 411014 has 4 localities; 853204 has 34 spread
    across 5 districts and several taluks, so each line differs beyond the
    locality name.
    """
    if not (pin.isdigit() and len(pin) == 6):
        raise HTTPException(400, "pincode must be 6 digits")
    d = db()
    try:
        head = d.rows("""SELECT primary_district, primary_state, n_districts, n_states,
                                is_deliverable, status, centroid_lat, centroid_lon
                         FROM pincode WHERE pincode = ?""", (pin,))
        if not head:
            raise HTTPException(404, f"PIN {pin} not found")
        head = head[0]
        rows = d.rows("""SELECT office_name, office_type, delivery_status, taluk,
                                district_raw, state_raw, circle_name, region_name,
                                division_name
                         FROM post_office WHERE pincode = ? AND valid_to IS NULL
                         ORDER BY office_name""", (pin,))
        out = []
        for r in rows:
            loc = _strip_office_suffix(r["office_name"])
            out.append({
                "locality": loc,
                "taluk": r["taluk"],
                "district": r["district_raw"],
                "state": r["state_raw"],
                "pincode": pin,
                "formatted": format_address(loc, r["taluk"], r["district_raw"],
                                            r["state_raw"], pin),
                "post_office": r["office_name"],
                "office_type": r["office_type"],
                "deliverable": (r["delivery_status"] or "").lower().startswith("delivery"),
                "postal_circle": r["circle_name"],
                "postal_division": r["division_name"],
            })
        return {
            "pincode": pin,
            "state": head["primary_state"],
            "districts": sorted({r["district"] for r in out if r["district"]}),
            "taluks": sorted({r["taluk"] for r in out if r["taluk"]}),
            "straddles_districts": head["n_districts"] > 1,
            "straddles_states": head["n_states"] > 1,
            "status": head["status"],
            "centroid": None if head["centroid_lat"] is None
                        else [head["centroid_lat"], head["centroid_lon"]],
            "count": len(out),
            "addresses": out,
        }
    finally:
        d.close()


# ---------------------------------------------------------------- offices
@app.get("/api/offices")
def offices(q: str = "", pincode: str = "", state: str = "", district: str = "",
            office_type: str = "", closed: bool = False,
            limit: int = Query(50), offset: int = Query(0)):
    limit, offset = page(limit, offset)
    where = ["valid_to IS NOT NULL" if closed else "valid_to IS NULL"]
    args = []
    if pincode:
        where.append("pincode = ?"); args.append(pincode)
    if state:
        where.append("state_raw = ?"); args.append(state)
    if district:
        where.append("district_raw = ?"); args.append(district)
    if office_type:
        where.append("office_type = ?"); args.append(office_type)
    if q:
        where.append("(LOWER(office_name) LIKE ? OR pincode LIKE ?)")
        args += ["%" + q.lower() + "%", q + "%"]
    sql = " WHERE " + " AND ".join(where)
    d = db()
    try:
        total = d.q("SELECT COUNT(*) FROM post_office" + sql, args)
        rows = d.rows(
            """SELECT office_name, pincode, office_type, delivery_status, taluk,
                      district_raw AS district, state_raw AS state, circle_name,
                      division_name, latitude, longitude, valid_from, valid_to
               FROM post_office""" + sql +
            " ORDER BY pincode, office_name LIMIT ? OFFSET ?", args + [limit, offset])
        return {"total": total, "limit": limit, "offset": offset, "rows": rows}
    finally:
        d.close()


# ------------------------------------------------------------- localities
@app.get("/api/localities")
def localities(q: str = "", pincode: str = "",
               limit: int = Query(50), offset: int = Query(0)):
    limit, offset = page(limit, offset)
    where, args = [], []
    if pincode:
        where.append("pincode = ?"); args.append(pincode)
    if q:
        where.append("LOWER(locality_name) LIKE ?"); args.append(q.lower() + "%")
    sql = " WHERE " + " AND ".join(where) if where else ""
    d = db()
    try:
        total = d.q("SELECT COUNT(*) FROM v_pincode_localities" + sql, args)
        rows = d.rows("SELECT pincode, locality_name, locality_type, source_id "
                      "FROM v_pincode_localities" + sql +
                      " ORDER BY locality_name, pincode LIMIT ? OFFSET ?",
                      args + [limit, offset])
        return {"total": total, "limit": limit, "offset": offset, "rows": rows}
    finally:
        d.close()


@app.get("/api/search")
def search(q: str, limit: int = Query(25, ge=1, le=100)):
    """Spelling-tolerant place-name search.

    Tries three widening tiers and stops at the first that returns anything, so
    an exact match is never buried under fuzzy noise:
      1. the name as typed (prefix)
      2. fold_key  - same sound, different spelling (Kondwa -> Kondhwa)
      3. skel_key  - consonants only (Mohammadwadi -> Mahamadwadi)
    Results are then ranked by similarity to what was actually typed.
    """
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "q is required")

    # Resolve candidate localities from an indexed column FIRST, then join.
    # Filtering inside the 748k-row join instead cost ~800ms per keystroke.
    cap = max(limit * 8, 200)
    sel = f"""SELECT DISTINCT l.locality_name, lp.pincode, l.locality_type,
                     l.source_id, p.primary_district AS district,
                     p.primary_state AS state, p.is_deliverable, p.n_districts
              FROM locality l
              JOIN locality_pincode lp ON lp.locality_id = l.locality_id
                                      AND lp.valid_to IS NULL
              JOIN pincode p ON p.pincode = lp.pincode
              WHERE l.locality_id IN (
                    SELECT locality_id FROM locality WHERE {{}} LIMIT {cap})"""
    d = db()
    try:
        rows, tier = [], None
        for name, where, arg in (
                ("exact", "name_lower LIKE ?", q.lower() + "%"),
                ("fold",  "fold_key LIKE ?", fuzzy.fold(q) + "%"),
                ("skeleton", "skel_key = ?", fuzzy.skeleton(q))):
            rows = d.rows(sel.format(where), (arg,))
            if rows:
                tier = name
                break
        return {"query": q, "matched_by": tier, "total": len(rows),
                "results": [
                    {**r, "straddles_districts": bool((r.get("n_districts") or 0) > 1)}
                    for r in fuzzy.rank(q, rows, "locality_name", limit)]}
    finally:
        d.close()


@app.get("/api/lookup")
def lookup(q: str):
    """One call for an address form: resolve a PIN or a place name to PINs."""
    q = q.strip()
    if not q:
        raise HTTPException(400, "q is required")
    d = db()
    try:
        if q.isdigit():
            rows = d.rows("""SELECT pincode, primary_district AS district,
                                    primary_state AS state, is_deliverable,
                                    n_districts, centroid_lat, centroid_lon, status
                             FROM pincode WHERE pincode LIKE ?
                             ORDER BY pincode LIMIT 20""", (q + "%",))
        else:
            rows = d.rows("""SELECT DISTINCT p.pincode, l.locality_name,
                                    p.primary_district AS district,
                                    p.primary_state AS state, p.is_deliverable,
                                    p.n_districts, p.centroid_lat, p.centroid_lon,
                                    p.status
                             FROM v_pincode_localities l
                             JOIN pincode p ON p.pincode = l.pincode
                             WHERE LOWER(l.locality_name) LIKE ?
                             ORDER BY l.locality_name, p.pincode LIMIT 20""",
                          (q.lower() + "%",))
        for r in rows:
            # never let a caller treat a multi-district PIN as verified
            r["straddles_districts"] = bool(r.get("n_districts", 0) > 1)
        return {"query": q, "results": rows}
    finally:
        d.close()


# --------------------------------------------------------------- downloads
DOWNLOADS = {
    "pincode_master": ("SELECT * FROM pincode ORDER BY pincode", None),
    "post_offices": ("SELECT * FROM post_office WHERE valid_to IS NULL "
                     "ORDER BY pincode, office_name", None),
    "pincode_centroids": ("SELECT pincode, centroid_lat, centroid_lon, area_sqkm "
                          "FROM pincode WHERE centroid_lat IS NOT NULL ORDER BY pincode", None),
    "pincode_district_map": ("SELECT DISTINCT pincode, district_raw AS district, "
                             "state_raw AS state FROM post_office "
                             "WHERE valid_to IS NULL AND district_raw IS NOT NULL "
                             "ORDER BY pincode, district_raw", None),
    "pincode_locality_map": ("SELECT pincode, locality_name, locality_type, source_id "
                             "FROM v_pincode_localities ORDER BY pincode, locality_name", None),
    "qa_multi_district": ("SELECT pincode, primary_district, primary_state, n_districts, "
                          "n_states, n_offices FROM pincode WHERE n_districts > 1 "
                          "ORDER BY n_districts DESC, pincode", None),
    "qa_coverage_gaps": ("SELECT * FROM v_coverage_gaps ORDER BY pincode", None),
    "closed_offices": ("SELECT office_key, office_name, pincode, district_raw, state_raw, "
                       "valid_from, valid_to FROM post_office WHERE valid_to IS NOT NULL "
                       "ORDER BY valid_to DESC", None),
    "states": ("SELECT state_code, state_name, state_type FROM state ORDER BY state_name", None),
    "districts": ("SELECT s.state_name, d.district_name FROM district d "
                  "JOIN state s ON s.state_code=d.state_code "
                  "ORDER BY s.state_name, d.district_name", None),
    "change_log": ("SELECT * FROM change_log ORDER BY detected_at DESC, change_id DESC", None),
}


# Exports whose columns are computed in Python rather than SQL. The address
# string and the comma-joined area list both need logic that SQLite and
# Postgres spell differently (GROUP_CONCAT vs STRING_AGG), so they are built
# here and stream the same way.
def _rows_full_addresses(d):
    yield ["pincode", "locality", "taluk", "district", "state", "full_address",
           "post_office", "office_type", "deliverable"]
    for r in d.rows("""SELECT pincode, office_name, office_type, delivery_status,
                              taluk, district_raw, state_raw
                       FROM post_office WHERE valid_to IS NULL
                       ORDER BY pincode, office_name"""):
        loc = _strip_office_suffix(r["office_name"])
        yield [r["pincode"], loc, r["taluk"] or "", r["district_raw"] or "",
               r["state_raw"] or "",
               format_address(loc, r["taluk"], r["district_raw"], r["state_raw"],
                              r["pincode"]),
               r["office_name"], r["office_type"] or "",
               1 if (r["delivery_status"] or "").lower().startswith("delivery") else 0]


def _rows_pincode_areas(d):
    """One row per PIN with every area it covers - the 'Areas covered' column."""
    yield ["pincode", "primary_district", "primary_state", "areas_covered",
           "total_areas", "status"]
    by = {}
    for r in d.rows("""SELECT pincode, locality_name FROM v_pincode_localities
                       ORDER BY pincode, locality_name"""):
        by.setdefault(r["pincode"], []).append(r["locality_name"])
    meta = {r["pincode"]: r for r in d.rows(
        "SELECT pincode, primary_district, primary_state, status FROM pincode")}
    for pin in sorted(by):
        m = meta.get(pin, {})
        yield [pin, m.get("primary_district") or "", m.get("primary_state") or "",
               ", ".join(by[pin]), len(by[pin]), m.get("status") or ""]


def _rows_areas_wide(d, cols="auto"):
    """The console's wide layout: PIN | Area 1 | Area 2 | ... | District | State.

    cols="auto" (the default) fits the widest PIN in the export, so nothing is
    truncated. That is genuinely dynamic - unlike sizing to the rows visible on
    one screen, which would silently cut every PIN outside that page.

    Auto is bounded by AREA_COL_LIMIT only to stop a pathological future value;
    today the widest PIN (834001, Ranchi) covers ~1,016 areas, which Excel's
    16,384-column limit handles comfortably. Expect a large, sparse file.
    """
    if str(cols).strip().lower() == "auto":
        cols = d.q("""SELECT MAX(n) FROM (
                        SELECT COUNT(*) n FROM locality_pincode
                        WHERE valid_to IS NULL GROUP BY pincode) x""") or 1
    cols = max(1, min(int(cols), AREA_COL_LIMIT))
    head = ["pincode"] + [f"area_{i+1}" for i in range(cols)]
    head += ["district", "state", "total_areas", "offices", "status"]
    yield head

    by = {}
    for r in d.rows("""SELECT lp.pincode, l.locality_name,
                              dd.district_name AS district, ss.state_name AS state
                       FROM locality_pincode lp
                       JOIN locality l ON l.locality_id = lp.locality_id
                       LEFT JOIN district dd ON dd.district_id = l.district_id
                       LEFT JOIN state ss ON ss.state_code = dd.state_code
                       WHERE lp.valid_to IS NULL
                       ORDER BY lp.pincode, l.locality_name"""):
        by.setdefault(r["pincode"], []).append(r)

    meta = {r["pincode"]: r for r in d.rows(
        """SELECT pincode, primary_district, primary_state, n_offices, status
           FROM pincode""")}

    for pin in sorted(by):
        m = meta.get(pin, {})
        items = by[pin]
        # area name only; district and state are separate columns at the end
        cells = [items[i]["locality_name"] if i < len(items) else ""
                 for i in range(cols)]
        yield [pin] + cells + [m.get("primary_district") or "",
                               m.get("primary_state") or "", len(items),
                               m.get("n_offices") or 0, m.get("status") or ""]


COMPUTED = {
    "pincode_full_addresses": _rows_full_addresses,
    "pincode_areas": _rows_pincode_areas,
    "pincode_areas_wide": _rows_areas_wide,
}


def _coerce_cols(raw, default="auto", lo=1, hi=None):
    """Never reject a download over a query parameter.

    `cols` arrives from a plain <a href> that a stale cached page may have
    built, so it can be 'auto', 'undefined' or empty. Typing it as int made
    FastAPI answer 422 with JSON, and the browser saved the error body as
    pincode_areas_wide.json and reported 'file wasn't available'. Anything
    unparseable now falls back to the default instead.
    """
    txt = str(raw or "").strip().lower()
    if txt in ("", "auto", "undefined", "null", "nan"):
        return "auto"
    try:
        return max(lo, min(int(txt), hi or AREA_COL_LIMIT))
    except (TypeError, ValueError):
        return default


@app.get("/api/download/{name}.csv")
def download(name: str, cols: str = Query("auto")):
    if name not in DOWNLOADS and name not in COMPUTED:
        raise HTTPException(404, f"unknown export '{name}'. Available: "
                                 f"{', '.join(sorted(set(DOWNLOADS) | set(COMPUTED)))}")

    def gen():
        d = db()
        try:
            buf = io.StringIO()
            w = csv.writer(buf)
            if name in COMPUTED:
                fn = COMPUTED[name]
                it = (fn(d, _coerce_cols(cols)) if name == "pincode_areas_wide"
                      else fn(d))
                n = 0
                for row in it:
                    w.writerow(row)
                    n += 1
                    if n % 2000 == 0:
                        yield buf.getvalue()
                        buf.seek(0); buf.truncate(0)
                if buf.tell():
                    yield buf.getvalue()
                return
            cur = d.execute(DOWNLOADS[name][0])
            w.writerow([c[0] for c in cur.description])
            yield buf.getvalue()
            while True:
                batch = cur.fetchmany(2000)
                if not batch:
                    break
                buf.seek(0); buf.truncate(0)
                w.writerows(batch)
                yield buf.getvalue()
        finally:
            d.close()

    return StreamingResponse(
        gen(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


# What each export is for, in the order someone would actually reach for them.
# Row counts are read live so they never drift from the data.
CATALOGUE = [
    ("Use these in the app", [
        ("pincode_areas_wide",
         "PIN codes with every area as its own column",
         "One row per PIN: area_1, area_2 … then district and state. The wide "
         "layout from the PIN codes tab. Column count follows the widest PIN.",
         "SELECT COUNT(DISTINCT pincode) FROM v_pincode_localities"),
        ("pincode_areas",
         "PIN codes with their areas in one cell",
         "Same data, areas comma-separated in a single column. Easier to import "
         "as a lookup table; harder to search inside.",
         "SELECT COUNT(DISTINCT pincode) FROM v_pincode_localities"),
        ("pincode_full_addresses",
         "One complete postal address per area",
         "Area, taluk, district, state and PIN assembled into a ready-to-display "
         "line. Use when you need the whole address, not just the area name.",
         "SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL"),
        ("pincode_master",
         "The PIN code table itself",
         "Every PIN with its primary office, district, state, office counts, "
         "deliverability, centroid, area and status.",
         "SELECT COUNT(*) FROM pincode"),
    ]),
    ("Reference data", [
        ("post_offices",
         "Every live post office",
         "Name, type (HO/SO/BO), delivery status, taluk, district, state, "
         "circle, division and coordinates.",
         "SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL"),
        ("pincode_locality_map",
         "Area to PIN, one row per pair",
         "The normalised form: searchable, indexable, and each row carries the "
         "source it came from. Use this over the comma-separated version.",
         "SELECT COUNT(*) FROM v_pincode_localities"),
        ("pincode_district_map",
         "PIN to district, the many-to-many truth",
         "Use this rather than a PIN's primary district - 1,256 PINs span more "
         "than one.",
         """SELECT COUNT(*) FROM (SELECT DISTINCT pincode, district_raw, state_raw
            FROM post_office WHERE valid_to IS NULL AND district_raw IS NOT NULL) x"""),
        ("pincode_centroids",
         "Latitude and longitude per PIN",
         "Centre point and area in km2, for distance and radius serviceability.",
         "SELECT COUNT(*) FROM pincode WHERE centroid_lat IS NOT NULL"),
        ("states", "States and union territories",
         "Canonical names and codes for dropdowns.",
         "SELECT COUNT(*) FROM state"),
        ("districts", "Districts with their state",
         "Canonical district names for dropdowns.",
         "SELECT COUNT(*) FROM district"),
    ]),
    ("Check before you ship", [
        ("qa_multi_district",
         "PINs that span more than one district",
         "The addresses that break auto-fill. Test your onboarding form against "
         "this list, not against Mumbai and Pune.",
         "SELECT COUNT(*) FROM pincode WHERE n_districts > 1"),
        ("qa_coverage_gaps",
         "PINs with geometry but no office, or the reverse",
         "Where the boundary data and the office directory disagree.",
         "SELECT COUNT(*) FROM v_coverage_gaps"),
    ]),
    ("Audit trail", [
        ("closed_offices",
         "Offices that have closed",
         "Never deleted, only closed with a date - so an address captured years "
         "ago still resolves. Empty until a refresh closes something.",
         "SELECT COUNT(*) FROM post_office WHERE valid_to IS NOT NULL"),
        ("change_log",
         "Every field-level change ever recorded",
         "What changed, when, and from what to what. Empty until the first "
         "monthly refresh runs.",
         "SELECT COUNT(*) FROM change_log"),
    ]),
]


@app.get("/api/downloads")
def download_list():
    d = db()
    try:
        out = []
        for group, items in CATALOGUE:
            entries = []
            for name, title, desc, count_sql in items:
                try:
                    n = d.q(count_sql)
                except Exception:
                    n = None
                entries.append({"name": name, "title": title, "description": desc,
                                "rows": n})
            out.append({"group": group, "items": entries})
        return out
    finally:
        d.close()


# ============================================================ pipeline control
# Running the pipeline from a browser needs three things to be safe:
#   1. a shared-secret token - without CK_ADMIN_TOKEN set, these endpoints do
#      not exist at all (404, so they are not even advertised);
#   2. an allowlist of tasks, so no caller-supplied string reaches a shell;
#   3. one job at a time, because two concurrent builds would fight over the
#      same rows.
# Jobs run as subprocesses of this container - the Docker socket is never
# mounted, which would be equivalent to handing out root on the host.
_jobs = {}
_job_ids = itertools.count(1)
_job_lock = threading.Lock()
_running = None


LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}
# Trust connections from the machine itself, so running locally or through an
# SSH tunnel needs no token. Set CK_TRUST_LOCAL=0 to require one even there.
TRUST_LOCAL = os.environ.get("CK_TRUST_LOCAL", "1").strip() not in ("0", "false", "no")

# Extra addresses allowed to run the pipeline without a token, e.g.
#   CK_TRUST_IPS=203.0.113.7,198.51.100.0/24
# Preferable to a token on a plain-HTTP host: an allowlist sends no secret over
# the wire at all, so there is nothing to intercept. Find your address with
# `curl ifconfig.me`. Note a home connection's address usually changes.
TRUST_IPS = [x.strip() for x in os.environ.get("CK_TRUST_IPS", "").split(",") if x.strip()]


def _trusted_peer(host):
    """Is this address on the allowlist? Accepts plain addresses and CIDRs."""
    if not host:
        return False
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in TRUST_IPS
    for entry in TRUST_IPS:
        try:
            if "/" in entry:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _is_local(request):
    """True when the peer is this machine, or an explicitly allowlisted address.

    Deliberately reads request.client, never X-Forwarded-For: that header is
    attacker-controlled, so trusting it would let anyone on the internet claim
    to be localhost. Behind a reverse proxy every request looks local, which is
    why TRUST_LOCAL must be set to 0 in that setup.
    """
    try:
        host = request.client.host if request.client else None
    except Exception:
        return False
    if host in LOOPBACK and TRUST_LOCAL:
        return True
    return _trusted_peer(host)


def _require_token(token, request=None):
    if request is not None and _is_local(request):
        return                      # same machine: no token needed
    if not ADMIN_TOKEN:
        # not configured and not local: behave as though the route does not exist
        raise HTTPException(404, "Not Found")
    if not token or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, "invalid or missing X-CK-Token")


def _finish(job, msg):
    """Fail a job before it starts, releasing the single-flight lock."""
    global _running
    job["status"] = "failed"
    job["exit_code"] = -1
    job["log"].append(msg)
    job["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _job_lock:
        _running = None


def _previewed_snapshot():
    try:
        with open(os.path.join(HERE, "reports", "last_refresh.json"),
                  encoding="utf-8") as f:
            return json.load(f).get("snapshot")
    except (OSError, ValueError):
        return None


def _run_job(job_id, name):
    job = _jobs[job_id]
    argv = [sys.executable, "-u"] + [os.path.join(HERE, TASKS[name]["argv"][0])] \
        + TASKS[name]["argv"][1:]
    if name == "refresh_apply":
        # Reuse the exact file the preview examined, so the delta that was
        # approved is the delta that gets written. Re-downloading could return a
        # different snapshot and apply changes nobody reviewed.
        snap = _previewed_snapshot()
        if not snap or not os.path.exists(snap):
            return _finish(job, "[!] no previewed snapshot to apply - "
                                "run the preview first")
        argv += ["--snapshot", snap]
    job["started"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        p = subprocess.Popen(argv, cwd=HERE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
        job["pid"] = p.pid
        for line in p.stdout:
            job["log"].append(line.rstrip("\n"))
            del job["log"][:-400]          # keep the tail bounded
        job["exit_code"] = p.wait()
        job["status"] = "ok" if job["exit_code"] == 0 else "failed"
    except Exception as e:                  # noqa: BLE001 - surfaced to the UI
        job["status"] = "failed"
        job["exit_code"] = -1
        job["log"].append(f"[!] {type(e).__name__}: {e}")
    finally:
        job["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        global _running
        with _job_lock:
            _running = None


@app.get("/api/admin/tasks")
def admin_tasks(request: Request, x_ck_token: str = Header(default="")):
    _require_token(x_ck_token, request)
    return {"tasks": [{"name": k, "label": v["label"], "writes": v["writes"],
                       "rank": v["rank"], "desc": v["desc"]}
                      for k, v in sorted(TASKS.items(), key=lambda x: x[1]["rank"])
                      if not v.get("hidden")],
            "running": _running}


@app.post("/api/admin/run/{name}")
def admin_run(name: str, request: Request, confirm: bool = False,
              x_ck_token: str = Header(default="")):
    _require_token(x_ck_token, request)
    if name not in TASKS:
        raise HTTPException(404, f"unknown task '{name}'")
    if TASKS[name]["writes"] and not confirm and not _is_local(request):
        raise HTTPException(400, f"'{name}' modifies the master; pass confirm=true")

    global _running
    with _job_lock:
        if _running is not None:
            raise HTTPException(409, f"job {_running} is still running")
        job_id = next(_job_ids)
        _running = job_id
        _jobs[job_id] = {"id": job_id, "task": name, "status": "running",
                         "log": [], "exit_code": None, "started": None,
                         "finished": None,
                         "queued": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    threading.Thread(target=_run_job, args=(job_id, name), daemon=True).start()
    return {"job": job_id, "task": name, "status": "running"}


@app.get("/api/admin/job/{job_id}")
def admin_job(job_id: int, request: Request, x_ck_token: str = Header(default="")):
    _require_token(x_ck_token, request)
    if job_id not in _jobs:
        raise HTTPException(404, f"no job {job_id}")
    j = dict(_jobs[job_id])
    j["log"] = j["log"][-200:]
    return j


@app.get("/api/admin/jobs")
def admin_jobs(request: Request, x_ck_token: str = Header(default="")):
    _require_token(x_ck_token, request)
    return {"running": _running,
            "jobs": [{k: v for k, v in j.items() if k != "log"}
                     for j in sorted(_jobs.values(), key=lambda x: -x["id"])[:20]]}


# How each source is meant to be kept current, and what it feeds.
SOURCE_INFO = {
    "datagov_directory": ("Post office directory",
                          "Offices, PINs, districts, coordinates", 30),
    "lgd_villages":      ("Village layer", "Area names for PIN lookup", 90),
    "pincode_boundary":  ("Boundary polygons", "Centroids, areas, map shapes", 365),
    "bulk_snapshot":     ("Bootstrap snapshot", "Superseded once the API pull runs", None),
    "lgd_local_bodies":  ("Local bodies", "Urban body to PIN", 90),
}


def _age_days(iso):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).days
    except (TypeError, ValueError):
        return None


@app.get("/api/pipeline/preview")
def pipeline_preview():
    """The delta the last preview found, so the console can show what WOULD
    change before anyone confirms an apply. Written by refresh.py alongside the
    markdown report."""
    p = os.path.join(HERE, "reports", "last_refresh.json")
    if not os.path.exists(p):
        return {"available": False}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"available": False}
    d["available"] = True
    # only an unapplied preview is a candidate for "apply these changes"
    d["applicable"] = bool(d.get("snapshot") and not d.get("applied")
                           and not d.get("blocked")
                           and os.path.exists(d.get("snapshot") or ""))
    d["age_days"] = _age_days(d.get("generated_at"))
    return d


@app.get("/api/pipeline/status")
def pipeline_status(request: Request):
    """Read-only, no token. Answers two questions: is the data still current,
    and is it internally consistent."""
    d = db()
    try:
        sources = []
        for r in d.rows("""SELECT source_id, MAX(fetched_at) AS last_fetched,
                                  COUNT(*) AS n
                           FROM snapshot GROUP BY source_id
                           ORDER BY MAX(fetched_at) DESC"""):
            label, feeds, every = SOURCE_INFO.get(
                r["source_id"], (r["source_id"], "", None))
            age = _age_days(r["last_fetched"])
            state = "ok"
            if every and age is not None:
                if age > every * 1.5:
                    state = "stale"
                elif age > every:
                    state = "due"
            sources.append({"source_id": r["source_id"], "label": label,
                            "feeds": feeds, "last_fetched": r["last_fetched"],
                            "age_days": age, "refresh_every_days": every,
                            "state": state, "snapshots": r["n"]})

        # the same assertions tools/verify.py makes, surfaced where people look
        checks = [
            ("Rollup agrees with post_office",
             d.q("""SELECT COUNT(*) FROM (
                      SELECT p.pincode FROM pincode p
                      LEFT JOIN post_office o
                        ON o.pincode = p.pincode AND o.valid_to IS NULL
                      GROUP BY p.pincode, p.n_offices
                      HAVING p.n_offices <> COUNT(o.office_id)) x"""), 0, "fail"),
            ("No retired PIN marked deliverable",
             d.q("""SELECT COUNT(*) FROM pincode WHERE status='retired'
                    AND (is_deliverable=1 OR n_offices>0)"""), 0, "fail"),
            ("One open row per office",
             d.q("""SELECT COUNT(*) FROM (
                      SELECT office_key FROM post_office WHERE valid_to IS NULL
                      GROUP BY office_key HAVING COUNT(*) > 1) x"""), 0, "fail"),
            ("Active PINs with no coordinates",
             d.q("""SELECT COUNT(*) FROM pincode
                    WHERE status='active' AND centroid_lat IS NULL"""), 0, "warn"),
            ("Active PINs with no area name",
             d.q("""SELECT COUNT(*) FROM pincode p WHERE p.status='active'
                    AND NOT EXISTS (SELECT 1 FROM locality_pincode lp
                                    WHERE lp.pincode = p.pincode
                                      AND lp.valid_to IS NULL)"""), 0, "warn"),
        ]
        checks = [{"label": l, "value": v, "expect": e,
                   "state": "pass" if v == e else sev}
                  for l, v, e, sev in checks]

        directory = next((s for s in sources
                          if s["source_id"] == "datagov_directory"), None)
        return {
            "admin_enabled": bool(ADMIN_TOKEN) or _is_local(request),
            "local_trusted": _is_local(request),
            "running_job": _running,
            "sources": sources,
            "checks": checks,
            "next_refresh_in_days": (None if not directory or directory["age_days"] is None
                                     else 30 - directory["age_days"]),
            "changes": d.q("SELECT COUNT(*) FROM change_log"),
            "last_change": d.q("SELECT MAX(detected_at) FROM change_log"),
            "recent": d.rows(
                """SELECT detected_at, entity, entity_key, change_type, field,
                          old_value, new_value
                   FROM change_log ORDER BY change_id DESC LIMIT 15"""),
        }
    finally:
        d.close()


if __name__ == "__main__":
    import uvicorn
    print(f"[i] serving {ckdb.describe()} on http://0.0.0.0:8000", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                log_level="info")
