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
import csv, io, os, sys

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
except ImportError:
    sys.exit("[!] pip install fastapi uvicorn")

import ckdb

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "web", "ui.html")
MAX_PAGE = 500

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
        where.append("(pincode LIKE ? OR LOWER(primary_office) LIKE ? "
                     "OR LOWER(primary_district) LIKE ?)")
        args += [q + "%", "%" + q.lower() + "%", "%" + q.lower() + "%"]
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


@app.get("/api/download/{name}.csv")
def download(name: str):
    if name not in DOWNLOADS:
        raise HTTPException(404, f"unknown export '{name}'. "
                                 f"Available: {', '.join(sorted(DOWNLOADS))}")
    sql, _ = DOWNLOADS[name]

    def gen():
        d = db()
        try:
            cur = d.execute(sql)
            cols = [c[0] for c in cur.description]
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
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


@app.get("/api/downloads")
def download_list():
    return sorted(DOWNLOADS)


if __name__ == "__main__":
    import uvicorn
    print(f"[i] serving {ckdb.describe()} on http://0.0.0.0:8000", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                log_level="info")
