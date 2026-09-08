-- ============================================================================
-- CK App / CK System - India PIN code master schema (SQLite; Postgres notes inline)
-- ----------------------------------------------------------------------------
-- Design rules
--  1. post_office is the grain of truth. India Post publishes offices, not PINs.
--  2. pincode is a DERIVED rollup. One PIN -> many offices, one office -> one PIN.
--  3. locality/village -> PIN is many-to-many. Never store it on the PIN row.
--  4. Nothing is hard-deleted. Rows are closed with valid_to so old addresses
--     captured in CK records still resolve (critical for KYC / audit trails).
--  5. Every row carries source_id + snapshot_id so any value is traceable to a
--     dated government file. Required if CK is ever RBI/audit inspected.
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- provenance
CREATE TABLE IF NOT EXISTS source (
    source_id       TEXT PRIMARY KEY,        -- 'datagov_directory', 'lgd_villages', ...
    publisher       TEXT NOT NULL,
    dataset_title   TEXT NOT NULL,
    resource_id     TEXT,                    -- data.gov.in resource UUID
    url             TEXT NOT NULL,
    licence         TEXT,
    authoritative   INTEGER NOT NULL DEFAULT 0  -- 1 = usable as audit evidence
);

CREATE TABLE IF NOT EXISTS snapshot (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL REFERENCES source(source_id),
    fetched_at      TEXT NOT NULL,           -- ISO8601 UTC
    published_at    TEXT,                    -- dataset's own "updated" date
    row_count       INTEGER,
    sha256          TEXT,                    -- hash of the raw payload
    notes           TEXT
);

-- ------------------------------------------------------------- reference dims
CREATE TABLE IF NOT EXISTS state (
    state_code      TEXT PRIMARY KEY,        -- LGD state code where known, else slug
    state_name      TEXT NOT NULL UNIQUE,    -- canonical title case
    state_type      TEXT                     -- 'State' | 'UT'
);

CREATE TABLE IF NOT EXISTS district (
    district_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    state_code      TEXT NOT NULL REFERENCES state(state_code),
    district_name   TEXT NOT NULL,           -- canonical title case
    lgd_code        TEXT,
    UNIQUE (state_code, district_name)
);

-- name_raw -> canonical resolver. This table is the whole reason address
-- matching works; India Post spells the same district 4 different ways.
CREATE TABLE IF NOT EXISTS name_alias (
    entity          TEXT NOT NULL,           -- 'state' | 'district' | 'taluk'
    name_raw        TEXT NOT NULL,
    name_canonical  TEXT NOT NULL,
    state_code      TEXT,
    PRIMARY KEY (entity, name_raw, state_code)
);

-- ---------------------------------------------------------------- core grain
CREATE TABLE IF NOT EXISTS post_office (
    office_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    office_key      TEXT NOT NULL,           -- normalise(officename)||'|'||pincode
    office_name     TEXT NOT NULL,
    pincode         TEXT NOT NULL,
    office_type     TEXT,                    -- HO | SO | BO
    delivery_status TEXT,                    -- Delivery | Non-Delivery
    circle_name     TEXT,
    region_name     TEXT,
    division_name   TEXT,
    taluk           TEXT,
    district_id     INTEGER REFERENCES district(district_id),
    district_raw    TEXT,
    state_raw       TEXT,
    telephone       TEXT,
    related_so      TEXT,
    related_ho      TEXT,
    latitude        REAL,
    longitude       REAL,
    source_id       TEXT NOT NULL REFERENCES source(source_id),
    snapshot_id     INTEGER NOT NULL REFERENCES snapshot(snapshot_id),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,                    -- NULL = current
    UNIQUE (office_key, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_po_pincode  ON post_office(pincode) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_po_name     ON post_office(office_name);
CREATE INDEX IF NOT EXISTS ix_po_current  ON post_office(valid_to);
-- At most ONE open row per office. This is the upsert target for build/refresh
-- and it structurally prevents duplicate "current" rows for the same office.
CREATE UNIQUE INDEX IF NOT EXISTS ux_po_current
    ON post_office(office_key) WHERE valid_to IS NULL;

-- ------------------------------------------------------------- derived rollup
CREATE TABLE IF NOT EXISTS pincode (
    pincode         TEXT PRIMARY KEY,
    postal_zone     TEXT,                    -- digit 1
    sub_zone        TEXT,                    -- digits 1-2 (circle)
    sorting_district TEXT,                   -- digits 1-3
    n_offices       INTEGER NOT NULL DEFAULT 0,
    n_delivery      INTEGER NOT NULL DEFAULT 0,
    is_deliverable  INTEGER NOT NULL DEFAULT 0,
    primary_office  TEXT,                    -- HO > SO > BO pick
    primary_district TEXT,
    primary_state   TEXT,
    n_districts     INTEGER NOT NULL DEFAULT 0,  -- >1 means PIN straddles districts
    n_states        INTEGER NOT NULL DEFAULT 0,  -- >1 is rare but real
    centroid_lat    REAL,
    centroid_lon    REAL,
    min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
    area_sqkm       REAL,
    has_boundary    INTEGER NOT NULL DEFAULT 0,
    is_army_postal  INTEGER NOT NULL DEFAULT 0,  -- zone 9 APS
    first_seen      TEXT,
    last_seen       TEXT,
    status          TEXT NOT NULL DEFAULT 'active'  -- active | retired
);
CREATE INDEX IF NOT EXISTS ix_pin_state ON pincode(primary_state);
CREATE INDEX IF NOT EXISTS ix_pin_geo   ON pincode(centroid_lat, centroid_lon);

-- Postgres: replace the four bbox cols with
--   geom geometry(MultiPolygon,4326), centroid geometry(Point,4326)
--   CREATE INDEX ON pincode USING GIST (geom);
CREATE TABLE IF NOT EXISTS pincode_boundary (
    pincode         TEXT PRIMARY KEY REFERENCES pincode(pincode),
    geojson         TEXT NOT NULL,           -- simplified geometry
    n_parts         INTEGER,
    source_id       TEXT REFERENCES source(source_id),
    snapshot_id     INTEGER REFERENCES snapshot(snapshot_id)
);

-- --------------------------------------------------------- locality (M:N)
CREATE TABLE IF NOT EXISTS locality (
    locality_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    locality_key    TEXT NOT NULL,           -- normalise(name)|lgd_code|district_id
    locality_name   TEXT NOT NULL,
    locality_type   TEXT,                    -- village | town | local_body | office_name
    lgd_code        TEXT NOT NULL DEFAULT '',
    district_id     INTEGER REFERENCES district(district_id),
    source_id       TEXT REFERENCES source(source_id)
);
-- Identity lives in locality_key, NOT in a UNIQUE over nullable columns. SQL
-- treats NULLs as distinct, so UNIQUE(locality_name, lgd_code, district_id)
-- silently failed to dedupe whenever lgd_code or district_id was NULL:
-- inserting 'Kharadi' three times with INSERT OR IGNORE produced three rows.
-- Every loader must build the key with a sentinel for missing parts.
CREATE UNIQUE INDEX IF NOT EXISTS ux_locality_key ON locality(locality_key);
CREATE INDEX IF NOT EXISTS ix_locality_name ON locality(locality_name);
CREATE TABLE IF NOT EXISTS locality_pincode (
    locality_id     INTEGER NOT NULL REFERENCES locality(locality_id),
    pincode         TEXT NOT NULL,
    source_id       TEXT NOT NULL REFERENCES source(source_id),
    snapshot_id     INTEGER NOT NULL REFERENCES snapshot(snapshot_id),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,
    PRIMARY KEY (locality_id, pincode, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_locpin_pin ON locality_pincode(pincode);
-- At most ONE open link per (locality, pincode). valid_from is part of the PK,
-- so a fresh timestamp on each run made ON CONFLICT DO NOTHING a no-op and the
-- link table doubled on every reload. This index is the real conflict target.
CREATE UNIQUE INDEX IF NOT EXISTS ux_locpin_current
    ON locality_pincode(locality_id, pincode) WHERE valid_to IS NULL;

-- ------------------------------------------------------------ change tracking
CREATE TABLE IF NOT EXISTS change_log (
    change_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT NOT NULL,
    snapshot_id     INTEGER REFERENCES snapshot(snapshot_id),
    entity          TEXT NOT NULL,           -- 'pincode' | 'post_office' | 'locality_pincode'
    entity_key      TEXT NOT NULL,
    change_type     TEXT NOT NULL,           -- added | removed | modified
    field           TEXT,
    old_value       TEXT,
    new_value       TEXT
);
CREATE INDEX IF NOT EXISTS ix_chg_time ON change_log(detected_at);
CREATE INDEX IF NOT EXISTS ix_chg_key  ON change_log(entity, entity_key);

-- ----------------------------------------------------------------- app views
-- What the CK App checkout / onboarding form actually calls.
CREATE VIEW IF NOT EXISTS v_pincode_lookup AS
SELECT p.pincode,
       p.primary_office      AS office_name,
       p.primary_district    AS district,
       p.primary_state       AS state,
       p.n_offices,
       p.is_deliverable,
       p.centroid_lat, p.centroid_lon,
       CASE WHEN p.n_districts > 1 THEN 1 ELSE 0 END AS straddles_districts,
       p.is_army_postal,
       p.status
FROM pincode p;

-- Every office behind a PIN, for "select your nearest post office" UX.
CREATE VIEW IF NOT EXISTS v_pincode_offices AS
SELECT pincode, office_name, office_type, delivery_status, taluk,
       district_raw AS district, state_raw AS state, latitude, longitude
FROM post_office
WHERE valid_to IS NULL;

-- Place-name -> PIN, the "type Viman Nagar instead of 411014" lookup.
-- source_id travels with every row: an ODbL/OSM-derived name must stay
-- distinguishable from a GODL one, because their licences differ.
CREATE VIEW IF NOT EXISTS v_pincode_localities AS
SELECT lp.pincode,
       l.locality_name,
       l.locality_type,
       l.district_id,
       lp.source_id
FROM locality_pincode lp
JOIN locality l ON l.locality_id = lp.locality_id
WHERE lp.valid_to IS NULL;

-- Coverage QA: PINs with offices but no polygon, or vice versa.
CREATE VIEW IF NOT EXISTS v_coverage_gaps AS
SELECT pincode, n_offices, has_boundary, status
FROM pincode
WHERE (n_offices = 0 AND has_boundary = 1)
   OR (n_offices > 0 AND has_boundary = 0);
