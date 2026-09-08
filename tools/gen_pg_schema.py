#!/usr/bin/env python3
"""
Generate schema.pg.sql from the canonical schema.sql.

    python tools/gen_pg_schema.py

schema.sql stays the single source of truth; this writes the translated
PostgreSQL form so it can be reviewed in a diff, fed to psql by hand, or used by
a migration tool. build_db.py does NOT read this file - it translates on the fly
- so the two can never silently diverge.
"""
import os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ckdb  # noqa: E402

OUT = os.path.join(HERE, "schema.pg.sql")


def main():
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        src = f.read()
    pg = ckdb._pg_ddl(src)
    stmts = ckdb.split_statements(pg)
    header = (
        "-- GENERATED FILE - do not edit by hand.\n"
        "-- Source: schema.sql   Regenerate: python tools/gen_pg_schema.py\n"
        "--\n"
        "-- build_db.py translates schema.sql on the fly and never reads this file;\n"
        "-- it exists for code review and for applying the schema with psql directly.\n\n")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(header + ";\n\n".join(s.strip() for s in stmts) + ";\n")
    print(f"[done] {len(stmts)} statements -> {OUT}")


if __name__ == "__main__":
    main()
