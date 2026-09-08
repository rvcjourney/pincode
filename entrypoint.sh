#!/bin/sh
# CK PIN Code Master - container entrypoint.
#
#   docker compose run --rm etl bootstrap  rebuild raw/ from the committed exports
#   docker compose run --rm etl build      upsert the master (safe, idempotent)
#   docker compose run --rm etl localities place-name -> PIN layer
#   docker compose run --rm etl refresh    monthly diff + change_log + report
#   docker compose run --rm etl export     regenerate flat files + QA summary
#   docker compose run --rm etl verify     integrity checks only, read-only
#   docker compose run --rm etl test       run the regression suite
#   docker compose run --rm etl all        bootstrap + build + localities + export + verify
#   docker compose run --rm etl shell      drop into a shell
#
# No command here ever drops or recreates the database. `build_db.py --fresh`
# exists but additionally requires --yes-wipe, so it cannot happen by accident.
set -e
cmd="${1:-build}"
shift 2>/dev/null || true

case "$cmd" in
  bootstrap)  exec python tools/bootstrap_raw.py "$@" ;;
  build)      exec python build_db.py "$@" ;;
  localities) exec python build_localities.py "$@" ;;
  refresh)    exec python refresh.py  "$@" ;;
  export)     exec python export.py   "$@" ;;
  geo)        exec python build_geo.py "$@" ;;
  fetch)      exec python fetch_datagov.py "$@" ;;
  verify)     exec python tools/verify.py "$@" ;;
  explorer)   exec python tools/build_explorer.py "$@" ;;
  test)       exec python tests/test_pipeline.py "$@" ;;
  # First-run convenience: everything needed to go from a fresh clone to a
  # populated, verified database without an API key. Each step is idempotent,
  # so re-running `all` is safe.
  all)
    set -e
    [ -f raw/postoffices.jsonl ] || [ -f raw/mirror_pincode.csv ] \
      || python tools/bootstrap_raw.py
    python build_db.py
    python build_localities.py
    python export.py
    exec python tools/verify.py
    ;;
  shell)      exec /bin/sh ;;
  *)          exec "$cmd" "$@" ;;
esac
