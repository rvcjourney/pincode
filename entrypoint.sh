#!/bin/sh
# CK PIN Code Master - container entrypoint.
#
#   docker compose run --rm etl build     upsert the master (safe, idempotent)
#   docker compose run --rm etl refresh   monthly diff + change_log + report
#   docker compose run --rm etl export    regenerate flat files + QA summary
#   docker compose run --rm etl verify    integrity checks only, read-only
#   docker compose run --rm etl test      run the regression suite
#   docker compose run --rm etl shell     drop into a shell
#
# No command here ever drops or recreates the database. `build_db.py --fresh`
# exists but additionally requires --yes-wipe, so it cannot happen by accident.
set -e
cmd="${1:-build}"
shift 2>/dev/null || true

case "$cmd" in
  build)   exec python build_db.py "$@" ;;
  refresh) exec python refresh.py  "$@" ;;
  export)  exec python export.py   "$@" ;;
  geo)     exec python build_geo.py "$@" ;;
  fetch)   exec python fetch_datagov.py "$@" ;;
  verify)  exec python tools/verify.py "$@" ;;
  test)    exec python tests/test_pipeline.py "$@" ;;
  shell)   exec /bin/sh ;;
  *)       exec "$cmd" "$@" ;;
esac
