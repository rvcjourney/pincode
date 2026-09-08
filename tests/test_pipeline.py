#!/usr/bin/env python3
"""
Self-contained regression tests for the CK PIN code pipeline.

    python tests/test_pipeline.py            # sqlite (default)
    CK_TEST_DB_URL=postgresql://... python tests/test_pipeline.py

No pytest required. Every test builds a throwaway database from tiny fixtures
and asserts on the result. Each one pins a bug that was actually shipped.
"""
import csv, json, os, subprocess, sys, tempfile, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ckdb          # noqa: E402
import rollup        # noqa: E402
from build_db import office_key, _f, _lat, _lon, canon_state, title  # noqa: E402

PY = sys.executable
MIRROR_COLS = ["officename", "pincode", "officeType", "Deliverystatus", "circlename",
               "regionname", "divisionname", "Taluk", "Districtname", "statename",
               "Telephone", "RelatedSuboffice", "RelatedHeadoffice"]

_results = []


def test(fn):
    _results.append(fn)
    return fn


# ------------------------------------------------------------------ utilities
def office(name, pin, typ="BO", delivery="Delivery", district="Thane",
           state="Maharashtra"):
    return {"officename": name, "pincode": pin, "officeType": typ,
            "Deliverystatus": delivery, "circlename": state, "regionname": "R",
            "divisionname": "D", "Taluk": "T", "Districtname": district,
            "statename": state, "Telephone": "", "RelatedSuboffice": "",
            "RelatedHeadoffice": ""}


def write_mirror(path, offices):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MIRROR_COLS)
        w.writeheader()
        for o in offices:
            w.writerow(o)


def write_geo(path, pins):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("pincode,area_sqkm,centroid_lat,centroid_lon,"
                "min_lat,min_lon,max_lat,max_lon,n_parts\n")
        for i, p in enumerate(pins):
            lat, lon = 19.0 + i * 0.01, 73.0 + i * 0.01
            f.write(f"{p},1.5,{lat},{lon},{lat-.01},{lon-.01},{lat+.01},{lon+.01},1\n")


def build(tmp, offices, geo_pins=(), extra=()):
    """Run the real build_db.py CLI against a throwaway database."""
    db_path = os.path.join(tmp, "t.db")
    mirror = os.path.join(tmp, "mirror.csv")
    write_mirror(mirror, offices)
    cmd = [PY, os.path.join(ROOT, "build_db.py"),
           "--db", f"sqlite:///{db_path}",
           "--offices", os.path.join(tmp, "nope.jsonl"),
           "--offices-csv", mirror,
           "--boundaries", os.path.join(tmp, "nope.geojson"),
           "--villages", os.path.join(tmp, "nope.jsonl")]
    geo = os.path.join(tmp, "geo.csv")
    if geo_pins:
        write_geo(geo, geo_pins)
        cmd += ["--geo", geo]
    else:
        cmd += ["--geo", os.path.join(tmp, "nope.csv")]
    cmd += list(extra)
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"build failed:\n{r.stdout}\n{r.stderr}"
    return f"sqlite:///{db_path}", r.stdout


# ---------------------------------------------------------------------- tests
@test
def t_office_key_keeps_type_suffix():
    """BUG 1: 'Airoli S.O' and 'Airoli B.O' are different offices sharing a PIN.
    The old key stripped the suffix, so one was silently dropped."""
    a = office_key("Airoli S.O", "400708")
    b = office_key("Airoli B.O", "400708")
    assert a != b, f"S.O and B.O still collide: {a}"
    # punctuation and case must still normalise away
    assert office_key("Airoli S.O", "400708") == office_key("airoli  s.o.", "400708")


@test
def t_both_offices_survive_collision():
    """BUG 1 end-to-end: both offices land in the table and n_offices agrees."""
    with tempfile.TemporaryDirectory() as tmp:
        url, _ = build(tmp, [office("Airoli S.O", "400708", "SO"),
                             office("Airoli B.O", "400708", "BO")])
        db = ckdb.connect(url)
        assert db.q("SELECT COUNT(*) FROM post_office") == 2, "an office was dropped"
        assert db.q("SELECT n_offices FROM pincode WHERE pincode='400708'") == 2
        db.close()


@test
def t_rollup_matches_actual_rows():
    """BUG 1 core: n_offices must be counted from post_office, never from the
    input list. A true duplicate is collapsed, and the rollup must agree."""
    with tempfile.TemporaryDirectory() as tmp:
        url, out = build(tmp, [office("Rampur B.O", "400708"),
                               office("Rampur B.O", "400708"),      # exact duplicate
                               office("Kalwa S.O", "400708", "SO")])
        assert "duplicate office_key" in out, "collision was not reported"
        db = ckdb.connect(url)
        actual = db.q("SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL")
        rolled = db.q("SELECT n_offices FROM pincode WHERE pincode='400708'")
        assert actual == rolled == 2, f"actual={actual} rollup={rolled}"
        db.close()


@test
def t_build_is_idempotent_and_keeps_history():
    """BUG 2: build_db used to os.remove() the database on every run, destroying
    change_log and every valid_to closure."""
    with tempfile.TemporaryDirectory() as tmp:
        offs = [office("A B.O", "400701"), office("B S.O", "400702", "SO")]
        url, _ = build(tmp, offs)
        db = ckdb.connect(url)
        db.execute("INSERT INTO change_log (detected_at, entity, entity_key, change_type) "
                   "VALUES ('2020-01-01','post_office','SENTINEL','modified')")
        db.commit()
        before = (db.q("SELECT COUNT(*) FROM post_office"),
                  db.q("SELECT COUNT(*) FROM pincode"))
        db.close()

        build(tmp, offs)          # same target, second run

        db = ckdb.connect(url)
        after = (db.q("SELECT COUNT(*) FROM post_office"),
                 db.q("SELECT COUNT(*) FROM pincode"))
        assert before == after, f"row counts moved: {before} -> {after}"
        assert db.q("SELECT COUNT(*) FROM change_log WHERE entity_key='SENTINEL'") == 1, \
            "history was wiped by a rebuild"
        db.close()


@test
def t_retired_pin_is_zeroed():
    """BUG 3: a PIN that loses its last office kept is_deliverable=1, so
    v_pincode_lookup still advertised a dead PIN as deliverable."""
    with tempfile.TemporaryDirectory() as tmp:
        url, _ = build(tmp, [office("Gone B.O", "400701"), office("Stay B.O", "400702")])
        db = ckdb.connect(url)
        assert db.q("SELECT is_deliverable FROM pincode WHERE pincode='400701'") == 1

        # close every office in 400701, then recompute exactly as refresh.py does
        db.execute("UPDATE post_office SET valid_to='2026-09-07' WHERE pincode='400701'")
        db.commit()
        rollup.recompute(db, "2026-09-07T00:00:00+00:00", verbose=False)
        db.commit()

        r = db.rows("SELECT * FROM pincode WHERE pincode='400701'")[0]
        assert r["status"] == "retired", r["status"]
        for f in ("n_offices", "n_delivery", "is_deliverable", "n_districts", "n_states"):
            assert r[f] == 0, f"{f} still {r[f]} on a retired PIN"
        for f in ("primary_office", "primary_district", "primary_state"):
            assert r[f] is None, f"{f} still {r[f]!r} on a retired PIN"
        # the surviving PIN is untouched
        assert db.q("SELECT is_deliverable FROM pincode WHERE pincode='400702'") == 1
        db.close()


@test
def t_retiring_preserves_geometry():
    """Retirement must zero office-derived columns only - never the polygon."""
    with tempfile.TemporaryDirectory() as tmp:
        url, _ = build(tmp, [office("Gone B.O", "400701")], geo_pins=["400701"])
        db = ckdb.connect(url)
        db.execute("UPDATE post_office SET valid_to='2026-09-07' WHERE pincode='400701'")
        db.commit()
        rollup.recompute(db, "2026-09-07T00:00:00+00:00", verbose=False)
        db.commit()
        r = db.rows("SELECT * FROM pincode WHERE pincode='400701'")[0]
        assert r["status"] == "retired"
        assert r["has_boundary"] == 1, "geometry flag was cleared"
        assert r["centroid_lat"] is not None, "centroid was wiped on retirement"
        assert r["area_sqkm"] is not None, "area was wiped on retirement"
        db.close()


@test
def t_coordinate_validation():
    """BUG 5: `x if -90 <= x <= 90 or True else None` never rejected anything."""
    assert _lat("12.97") == 12.97
    assert _lat("999") is None, "out-of-range latitude accepted"
    assert _lat("-500") is None
    assert _lat("1e400") is None, "infinity accepted"
    assert _lat("nan") is None, "NaN accepted"
    assert _lat("abc") is None
    assert _lat(None) is None
    assert _lon("77.5") == 77.5
    assert _lon("181") is None, "out-of-range longitude accepted"
    assert _lon("-179.9") == -179.9
    assert _f("5", 0, 10) == 5.0


@test
def t_first_seen_never_moves():
    """first_seen is provenance: it must survive every subsequent rebuild."""
    with tempfile.TemporaryDirectory() as tmp:
        offs = [office("A B.O", "400701")]
        url, _ = build(tmp, offs)
        db = ckdb.connect(url)
        db.execute("UPDATE pincode SET first_seen='2019-01-01T00:00:00+00:00'")
        db.commit()
        db.close()
        build(tmp, offs)
        db = ckdb.connect(url)
        assert db.q("SELECT first_seen FROM pincode WHERE pincode='400701'") \
            == "2019-01-01T00:00:00+00:00", "first_seen was overwritten"
        db.close()


@test
def t_close_missing_guard():
    """--close-missing must refuse a source that would close >2% of offices."""
    with tempfile.TemporaryDirectory() as tmp:
        offs = [office(f"O{i} B.O", f"4007{i:02d}") for i in range(50)]
        url, _ = build(tmp, offs)
        mirror = os.path.join(tmp, "mirror.csv")
        write_mirror(mirror, offs[:10])          # a truncated pull: 80% missing
        r = subprocess.run(
            [PY, os.path.join(ROOT, "build_db.py"), "--db", url,
             "--offices", os.path.join(tmp, "nope.jsonl"), "--offices-csv", mirror,
             "--geo", os.path.join(tmp, "nope.csv"),
             "--boundaries", os.path.join(tmp, "nope.geojson"),
             "--villages", os.path.join(tmp, "nope.jsonl"), "--close-missing"],
            capture_output=True, text=True)
        assert r.returncode != 0, "guard did not trip on a truncated source"
        assert ">2%" in (r.stdout + r.stderr)
        db = ckdb.connect(url)
        assert db.q("SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL") == 50, \
            "offices were closed despite the guard"
        db.close()


@test
def t_fresh_requires_explicit_confirmation():
    """--fresh is destructive and must not be usable by accident."""
    with tempfile.TemporaryDirectory() as tmp:
        url, _ = build(tmp, [office("A B.O", "400701")])
        mirror = os.path.join(tmp, "mirror.csv")
        r = subprocess.run(
            [PY, os.path.join(ROOT, "build_db.py"), "--db", url,
             "--offices", os.path.join(tmp, "nope.jsonl"), "--offices-csv", mirror,
             "--geo", os.path.join(tmp, "nope.csv"),
             "--boundaries", os.path.join(tmp, "nope.geojson"),
             "--villages", os.path.join(tmp, "nope.jsonl"), "--fresh"],
            capture_output=True, text=True)
        assert r.returncode != 0, "--fresh ran without --yes-wipe"
        db = ckdb.connect(url)
        assert db.q("SELECT COUNT(*) FROM post_office") == 1, "data destroyed anyway"
        db.close()


@test
def t_boundary_only_pins_are_retired_not_deliverable():
    """A PIN present only in the boundary file has no live office."""
    with tempfile.TemporaryDirectory() as tmp:
        url, _ = build(tmp, [office("A B.O", "400701")],
                       geo_pins=["400701", "400999"])
        db = ckdb.connect(url)
        r = db.rows("SELECT * FROM pincode WHERE pincode='400999'")[0]
        assert r["status"] == "retired"
        assert r["is_deliverable"] == 0 and r["n_offices"] == 0
        assert r["has_boundary"] == 1 and r["centroid_lat"] is not None
        db.close()


@test
def t_army_postal_flag_on_boundary_only_pin():
    """Zone 9 must be flagged even for PINs that arrive only via geometry."""
    with tempfile.TemporaryDirectory() as tmp:
        url, _ = build(tmp, [office("A B.O", "400701")], geo_pins=["900001"])
        db = ckdb.connect(url)
        assert db.q("SELECT is_army_postal FROM pincode WHERE pincode='900001'") == 1
        db.close()


@test
def t_normalisers():
    assert canon_state("ANDHRA PRADESH CIRCLE") == "Andhra Pradesh"
    assert canon_state("ORISSA") == "Odisha"
    assert canon_state("TAMILNADU") == "Tamil Nadu"
    assert canon_state("JAMMU AND KASHMIR") == "Jammu & Kashmir"
    assert canon_state(None) is None
    assert title("NORTH AND SOUTH 24 PARGANAS") == "North & South 24 Parganas"
    # canonicalisation must be idempotent - exports are re-read by bootstrap_raw
    assert canon_state(canon_state("ORISSA")) == "Odisha"
    assert title(title("BENGALURU URBAN")) == "Bengaluru Urban"


@test
def t_sql_translation_for_postgres():
    """The Postgres dialect shim, verified without needing a live server."""
    from ckdb import _qmark_to_pyformat as q, _pg_ddl
    assert q("SELECT * FROM t WHERE a=? AND b=?") == "SELECT * FROM t WHERE a=%s AND b=%s"
    # '?' inside a string literal is data, not a placeholder
    assert q("SELECT '?' , a FROM t WHERE b=?") == "SELECT '?' , a FROM t WHERE b=%s"
    # a literal % must be escaped for psycopg
    assert q("SELECT * FROM t WHERE a LIKE 'x%'") == "SELECT * FROM t WHERE a LIKE 'x%%'"
    ddl = _pg_ddl("PRAGMA foreign_keys = ON;\n"
                  "CREATE TABLE x (id INTEGER PRIMARY KEY AUTOINCREMENT, lat REAL);\n"
                  "CREATE VIEW IF NOT EXISTS v AS SELECT 1;")
    assert "PRAGMA" not in ddl
    assert "GENERATED BY DEFAULT AS IDENTITY" in ddl
    assert "DOUBLE PRECISION" in ddl and "REAL" not in ddl, \
        "pg REAL is float4 and would round coordinates"
    assert "CREATE OR REPLACE VIEW" in ddl


@test
def t_locality_name_extraction():
    from build_localities import locality_name as ln
    assert ln("Viman nagar S.O") == "Viman nagar"
    assert ln("Kharadi B.O") == "Kharadi"
    assert ln("Saket S.O (South Delhi)") == "Saket", ln("Saket S.O (South Delhi)")
    assert ln("Muppalla S.O (Guntur)") == "Muppalla"
    assert ln("Koramangala I Block S.O") == "Koramangala I Block"
    assert ln("New Delhi G.P.O.") == "New Delhi"
    assert ln("S.O") is None          # nothing left after the suffix
    assert ln("") is None
    assert ln("12") is None           # no letters


@test
def t_locality_key_dedupes_with_missing_parts():
    """The original UNIQUE(locality_name, lgd_code, district_id) did not dedupe
    when lgd_code/district_id were NULL - SQL treats NULLs as distinct, so
    inserting 'Kharadi' three times produced three rows. Identity now lives in
    locality_key, which substitutes a sentinel for every missing part."""
    from build_localities import key_of
    assert key_of("Kharadi", "", None) == key_of("Kharadi", "", None)
    assert key_of("Kharadi", "", None) != key_of("Kharadi", "", 7)
    assert key_of("Kharadi", "", None) != key_of("Kharadi", "LGD1", None)
    # normalisation: case and punctuation are not identity
    assert key_of("Viman Nagar", "", 1) == key_of("viman  nagar", "", 1)
    assert "None" not in key_of("X", None, None), "a NULL leaked into the key"


@test
def t_locality_load_is_idempotent():
    """Reloading must not duplicate localities OR their PIN links.

    valid_from is part of locality_pincode's PK, so a fresh timestamp on each
    run made ON CONFLICT DO NOTHING a no-op and the link table doubled every
    reload. The real conflict target is the partial index on open links."""
    with tempfile.TemporaryDirectory() as tmp:
        url, _ = build(tmp, [office("Viman nagar S.O", "411014", "SO"),
                             office("Vadgaon Sheri S.O", "411014", "SO"),
                             office("Kharadi B.O", "306308")])
        def load():
            r = subprocess.run([PY, os.path.join(ROOT, "build_localities.py"), "--db", url],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr
        load()
        db = ckdb.connect(url)
        first = (db.q("SELECT COUNT(*) FROM locality"),
                 db.q("SELECT COUNT(*) FROM locality_pincode WHERE valid_to IS NULL"))
        db.close()
        load(); load()
        db = ckdb.connect(url)
        again = (db.q("SELECT COUNT(*) FROM locality"),
                 db.q("SELECT COUNT(*) FROM locality_pincode WHERE valid_to IS NULL"))
        assert first == again, f"locality load not idempotent: {first} -> {again}"
        assert first[0] == 3, first
        # the view resolves a name to its PIN
        rows = db.rows("SELECT pincode FROM v_pincode_localities WHERE locality_name='Viman nagar'")
        assert [r["pincode"] for r in rows] == ["411014"], rows
        db.close()


@test
def t_pg_url_with_special_password_is_diagnosed():
    """`openssl rand -base64 32` emits '/', which ends the URL authority. libpq
    then reads part of the password as the hostname and dies with 'Servname not
    supported for ai_socktype'. Catch it with a message that names the cause."""
    from ckdb import pg_url_problem, pg_url

    bad = "postgresql://ck:R7x/Qm2Z+aB9c=@db:5432/ck_pincode"
    msg = pg_url_problem(bad)
    assert msg and "unencoded '/'" in msg, msg
    assert "PGPASSWORD" in msg, "the message must point at the actual fix"

    # a bare URL is legitimate - libpq supplies everything from PG* env vars
    assert pg_url_problem("postgresql://") is None
    assert pg_url_problem("postgresql://ck:plainpw@db:5432/ck_pincode") is None
    assert pg_url_problem("sqlite:///out/x.db") is None

    # a non-numeric port is the other symptom of the same class of breakage
    assert pg_url_problem("postgresql://ck:pw@db:notaport/x") is not None

    # the builder must produce something that survives a round trip
    from urllib.parse import urlsplit
    u = pg_url("db", "ck_pincode", "ck", "R7x/Qm2Z+aB9c=")
    assert pg_url_problem(u) is None, u
    s = urlsplit(u)
    assert s.hostname == "db" and s.port == 5432, (s.hostname, s.port)


@test
def t_describe_never_leaks_the_password():
    from ckdb import describe
    out = describe("postgresql://ck:sup3rs3cret@db:5432/ck_pincode")
    assert "sup3rs3cret" not in out, out
    assert "***" in out and "db:5432" in out, out


@test
def t_statement_splitter():
    from ckdb import split_statements
    s = split_statements("CREATE TABLE a (x TEXT DEFAULT 'a;b'); CREATE TABLE b (y TEXT);")
    assert len(s) == 2, s
    assert "'a;b'" in s[0]


@test
def t_splitter_ignores_apostrophes_in_comments():
    """An apostrophe inside a '--' comment must not open a string literal.

    schema.sql documents columns as `-- 'datagov_directory', 'lgd_villages'`.
    Treating those as quotes desynchronised the parser and collapsed the whole
    schema into 3 unusable statements - the Postgres deploy would have failed on
    the very first run."""
    from ckdb import split_statements
    s = split_statements(
        "CREATE TABLE a (   -- 'foo', 'bar', ...\n"
        "  x TEXT);\n"
        "CREATE TABLE b (y TEXT);   -- it's fine\n"
        "CREATE TABLE c (z TEXT);")
    assert len(s) == 3, f"expected 3 statements, got {len(s)}: {s}"
    # a trailing comment-only fragment must not become a statement
    assert not split_statements("-- just a comment\n")


@test
def t_real_schema_translates_to_valid_pg_statements():
    """The full schema.sql must survive translation intact."""
    import ckdb as _c
    with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as f:
        pg = _c._pg_ddl(f.read())
    import re as _re
    stmts = _c.split_statements(pg)
    assert len(stmts) >= 20, f"only {len(stmts)} statements survived translation"
    # strip leading comment lines: statements keep their documentation
    codes = ["\n".join(_c._split_comment(l)[0] for l in s.splitlines()).strip().upper()
             for s in stmts]
    # assert by name, not by count, so adding a table or view doesn't fail this
    for t in ["SOURCE", "SNAPSHOT", "STATE", "DISTRICT", "NAME_ALIAS", "POST_OFFICE",
              "PINCODE", "PINCODE_BOUNDARY", "LOCALITY", "LOCALITY_PINCODE", "CHANGE_LOG"]:
        assert any(c.startswith("CREATE TABLE IF NOT EXISTS " + t + " (") for c in codes), \
            "missing table " + t
    for v in ["V_PINCODE_LOOKUP", "V_PINCODE_OFFICES", "V_PINCODE_LOCALITIES",
              "V_COVERAGE_GAPS"]:
        assert any(c.startswith("CREATE OR REPLACE VIEW " + v + " ") for c in codes), \
            "missing view " + v
    # the partial unique indexes that enforce "one open row" are load-bearing
    for i in ["UX_PO_CURRENT", "UX_LOCALITY_KEY", "UX_LOCPIN_CURRENT"]:
        assert any(i in c and c.startswith("CREATE UNIQUE INDEX") for c in codes), \
            "missing unique index " + i
    for c in codes:
        assert "AUTOINCREMENT" not in c
        assert "PRAGMA" not in c
        assert not _re.search(r"\bREAL\b", c), \
            "pg REAL is float4 and would round coordinates"
    # prose in comments must be left alone
    assert "but real" in pg, "a comment was mangled by the REAL substitution"


# --------------------------------------------------------------------- runner
def main():
    passed, failed = 0, []
    for fn in _results:
        name = fn.__name__[2:] if fn.__name__.startswith("t_") else fn.__name__
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {e}")
            failed.append((name, traceback.format_exc()))
    print(f"\n{passed} passed, {len(failed)} failed")
    for name, tb in failed:
        print(f"\n--- {name} ---\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
