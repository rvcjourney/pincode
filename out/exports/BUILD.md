# Rebuilding the master DB from this bundle

The 120 MB SQLite master and the 219/54 MB boundary GeoJSON files are excluded
from this bundle to keep it portable. Rebuild them in ~4 minutes:

    pip install shapely openpyxl
    # 1. raw sources
    curl -L -o raw/pincode_area.xlsx \
      "https://raw.githubusercontent.com/er-data-storage/postal-code-data/master/Derived%20Information/pincode_geographical_area.xlsx"
    curl -L -o raw/india-pincode.geojson \
      "https://media.githubusercontent.com/media/er-data-storage/postal-code-data/master/india-pincode.geojson"
    export DATA_GOV_KEY=<your free data.gov.in key>
    python fetch_datagov.py 5c2f62fe-5afa-4119-a499-fec9d604d5bd raw/postoffices.jsonl
    # 2. build
    python build_geo.py && python build_db.py && python export.py
