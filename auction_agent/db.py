"""SQLite storage: connections, schema v2, migrations and repository functions.

Design rules (see docs/MASTER_PLAN.md section 2):
- Connections are always closed (`connect()` is a context manager) and run in
  autocommit mode; writes go through `write_tx()` which issues BEGIN IMMEDIATE.
- On a fresh file `auto_vacuum=INCREMENTAL` is set *before* WAL, otherwise SQLite
  silently ignores it and the file never shrinks after purges.
- Lifecycle columns (last_seen_at, missed_snapshots, missing_since, lifecycle) are
  written only by the set-based snapshot apply, never by the COALESCE upserts.
- Every ``*_at`` value uses ``utc_iso()`` (``YYYY-MM-DDTHH:MM:SSZ``).
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .config import utc_iso

SCHEMA_VERSION = 3

_TS = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'"


def _ts(col: str, nullable: bool = True) -> str:
    if nullable:
        return f"{col} TEXT CHECK ({col} IS NULL OR {col} {_TS})"
    return f"{col} TEXT NOT NULL CHECK ({col} {_TS})"


SCHEMA_V2 = f"""
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE sales (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    house         TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'room' CHECK (kind IN ('room','online','multisite','other')),
    audience      TEXT NOT NULL DEFAULT 'public' CHECK (audience IN ('public','pro')),
    room          TEXT,
    title         TEXT,
    url           TEXT NOT NULL,
    {_ts('starts_at')},
    {_ts('ends_at')},
    status        TEXT NOT NULL DEFAULT 'upcoming' CHECK (status IN ('upcoming','closed')),
    expected_lots INTEGER,
    {_ts('first_seen_at', nullable=False)},
    {_ts('last_listed_at')},
    UNIQUE (house, external_id)
);

CREATE TABLE vehicles (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    house              TEXT NOT NULL,
    external_id        TEXT NOT NULL,
    category           TEXT NOT NULL DEFAULT 'other'
                       CHECK (category IN ('voiture','utilitaire','materiel','other')),
    vin_hmac           TEXT,
    brand              TEXT,
    model              TEXT,
    trim               TEXT,
    fuel               TEXT,
    gearbox            TEXT,
    body_type          TEXT,
    first_registration TEXT,              -- ISO date YYYY-MM-DD
    year               INTEGER,
    mileage_km         INTEGER CHECK (mileage_km IS NULL OR mileage_km BETWEEN 0 AND 1500000),
    engine_cc          INTEGER,
    power_hp           INTEGER,
    co2                INTEGER,
    vat_regime         TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (vat_regime IN ('ttc','ht_recuperable','marge','unknown')),
    battery_soh_pct    REAL,
    battery_leased     INTEGER,
    warranty           TEXT,
    comments           TEXT,              -- VIN/plate-redacted; NULLed at dormancy
    comments_sha256    TEXT,
    raw_specs_json     TEXT,              -- spec table without VIN/plate rows
    photo_count        INTEGER,
    storage_location   TEXT,
    ingest_state       TEXT NOT NULL DEFAULT 'stub' CHECK (ingest_state IN ('stub','complete','failed')),
    ingest_attempts    INTEGER NOT NULL DEFAULT 0,
    {_ts('next_attempt_at')},
    last_error         TEXT,
    {_ts('detail_fetched_at')},
    ct_state           TEXT CHECK (ct_state IS NULL OR ct_state IN ('present','absent','failed')),
    lifecycle          TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active','dormant')),
    outcome            TEXT CHECK (outcome IS NULL OR outcome IN ('sold','not_sold','unknown')),
    {_ts('first_seen_at', nullable=False)},
    {_ts('last_seen_at', nullable=False)},
    {_ts('missing_since')},
    missed_snapshots   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (house, external_id)
);
CREATE INDEX ix_vehicles_vin ON vehicles (house, vin_hmac) WHERE vin_hmac IS NOT NULL;
CREATE INDEX ix_vehicles_lifecycle ON vehicles (house, lifecycle);

CREATE TABLE listings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id         INTEGER NOT NULL REFERENCES vehicles (id) ON DELETE CASCADE,
    sale_id            INTEGER NOT NULL REFERENCES sales (id) ON DELETE CASCADE,
    lot_number         INTEGER,
    detail_url         TEXT NOT NULL,
    starting_price     REAL,
    current_price_attr REAL,
    {_ts('closing_hint_at')},
    {_ts('first_seen_at', nullable=False)},
    {_ts('last_seen_at', nullable=False)},
    UNIQUE (vehicle_id, sale_id)
);
CREATE INDEX ix_listings_sale ON listings (sale_id);

CREATE TABLE documents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id       INTEGER NOT NULL REFERENCES vehicles (id) ON DELETE CASCADE,
    doc_type         TEXT NOT NULL CHECK (doc_type IN ('ct','maintenance','battery')),
    source_url       TEXT NOT NULL,
    sha256           TEXT,
    size_bytes       INTEGER,
    fetch_status     TEXT NOT NULL CHECK (fetch_status IN
                     ('ok','not_fetched','not_found','not_pdf','too_large','error')),
    parse_status     TEXT CHECK (parse_status IS NULL OR parse_status IN ('parsed','empty_text','failed')),
    parser_version   TEXT,
    parse_confidence REAL,
    text             TEXT,                -- VIN/plate-redacted; NULLed at dormancy
    parsed_json      TEXT,
    {_ts('fetched_at', nullable=False)},
    UNIQUE (vehicle_id, doc_type)
);

CREATE TABLE llm_assessments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id     INTEGER NOT NULL REFERENCES vehicles (id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('adjust','verdict','ct_extract')),
    llm_input_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    llm_model      TEXT NOT NULL,
    adjustment     INTEGER CHECK (adjustment IS NULL OR adjustment BETWEEN -15 AND 5),
    reasons_json   TEXT,
    verdict        TEXT,
    status         TEXT NOT NULL CHECK (status IN ('ok','failed')),
    error          TEXT,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    {_ts('created_at', nullable=False)}
);
CREATE INDEX ix_llm_lookup ON llm_assessments (vehicle_id, kind, llm_input_hash);

CREATE TABLE assessments (
    vehicle_id        INTEGER PRIMARY KEY REFERENCES vehicles (id) ON DELETE CASCADE,
    reliability       INTEGER NOT NULL,
    condition         INTEGER NOT NULL,
    value             INTEGER NOT NULL,
    base_score        INTEGER NOT NULL,
    llm_adjustment    INTEGER NOT NULL DEFAULT 0,
    final_score       INTEGER NOT NULL CHECK (final_score BETWEEN 0 AND 100),
    tier              TEXT NOT NULL CHECK (tier IN ('green','yellow','red')),
    max_tier          TEXT NOT NULL CHECK (max_tier IN ('green','yellow','red')),
    confidence        TEXT NOT NULL CHECK (confidence IN ('full','partial')),
    effective_price   REAL,
    value_basis       TEXT NOT NULL,
    gates_json        TEXT NOT NULL,
    flags_json        TEXT NOT NULL,
    reasons_json      TEXT NOT NULL,
    verdict           TEXT,
    heuristic_version TEXT NOT NULL,
    {_ts('scored_at', nullable=False)}
);

CREATE TABLE sync_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    house         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN
                  ('running','ok','incomplete','blocked','failed','purge_blocked')),
    scope_json    TEXT,
    {_ts('started_at', nullable=False)},
    {_ts('heartbeat_at')},
    {_ts('finished_at')},
    lots_seen     INTEGER,
    expected_lots INTEGER,
    new_count     INTEGER,
    relist_count  INTEGER,
    detailed_count INTEGER,
    documents_count INTEGER,
    failed_count  INTEGER,
    notes_json    TEXT,
    error         TEXT
);
CREATE UNIQUE INDEX one_running_per_house_kind ON sync_runs (house, kind) WHERE status = 'running';
"""


class LegacyDatabaseError(RuntimeError):
    pass


class RunLockedError(RuntimeError):
    pass


# --------------------------------------------------------------------------- connections


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a configured connection and always close it."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = (not db_path.exists()) or db_path.stat().st_size == 0
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        if fresh:
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"Could not enable WAL mode (got {mode!r})")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception (Ctrl+C included)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------- migrations


def is_legacy_v1(db_path: Path) -> bool:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False
    with closing(sqlite3.connect(str(db_path))) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != 0:
            return False
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('vehicles') WHERE name = 'detail_url'"
        ).fetchone()
        return row is not None


def retire_legacy_database(db_path: Path, now: Optional[datetime] = None) -> Optional[Path]:
    """Move a v1 prototype database aside after removing identifying data.

    The prototype stored VIN and plate columns, raw (unredacted) CT text, free-text
    comments and photo URLs. All of that is cleared before the file is kept as a
    backup; only specs, prices and scores remain. Returns the backup path, or None.
    """
    if not is_legacy_v1(db_path):
        return None
    with closing(sqlite3.connect(str(db_path))) as conn:

        def columns(table: str) -> set[str]:
            return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}

        scrub = {
            "vehicles": ("vin", "registration", "comments", "image_url"),
            "documents": ("extracted_text", "local_path"),
        }
        for table, wanted in scrub.items():
            present = columns(table)
            sets = [f"{c} = NULL" for c in wanted if c in present]
            if sets:
                conn.execute(f"UPDATE {table} SET {', '.join(sets)}")
        if columns("photos"):
            conn.execute("DELETE FROM photos")
        conn.commit()
        conn.execute("VACUUM")
    stamp = utc_iso(now).replace("-", "").replace(":", "")
    backup = db_path.with_name(f"auction.v1-{stamp}.db")
    try:
        os.replace(db_path, backup)
    except PermissionError as exc:  # Windows: file open in another program
        raise LegacyDatabaseError(
            f"Could not move the old database {db_path} aside. Close any program "
            "using it (DB browser, another crawl) and retry."
        ) from exc
    return backup


# Forward migrations: target version -> statements. Applied in order inside one transaction.
MIGRATIONS: dict[int, list[str]] = {
    3: [
        f"ALTER TABLE vehicles ADD COLUMN {_ts('late_ct_checked_at')}",
        "ALTER TABLE vehicles ADD COLUMN doc_attempts INTEGER NOT NULL DEFAULT 0",
    ],
}


def migrate(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema. Re-checks the version under the write lock, so two
    processes opening a new database at the same time cannot both run the DDL."""
    with write_tx(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema v{version} is newer than this code (v{SCHEMA_VERSION})")
        if version == 0:
            existing = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
            if existing:
                raise LegacyDatabaseError(
                    "Database has tables but no schema version; run `python -m auction_agent "
                    "cleanup-legacy` or point AUCTION_AGENT_DB to a new file."
                )
            for statement in _split_sql(SCHEMA_V2):
                conn.execute(statement)
            version = 2
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[target]:
                conn.execute(statement)
        if version != SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    _ensure_auto_vacuum(conn)


def _split_sql(script: str) -> list[str]:
    """Split a script into statements (comment- and string-aware via sqlite3)."""
    statements, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError("Incomplete SQL statement in schema")
    return statements


def _ensure_auto_vacuum(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("VACUUM")


def reclaim_space(conn: sqlite3.Connection, pages_per_step: int = 2000) -> int:
    """Return freed pages to the OS after deletes (auto_vacuum=INCREMENTAL)."""
    freed = 0
    while conn.execute("PRAGMA freelist_count").fetchone()[0] > 0:
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        conn.execute(f"PRAGMA incremental_vacuum({int(pages_per_step)})").fetchall()
        after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        freed += before - after
        if after >= before:
            break
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    return freed


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Connect and migrate. Raises LegacyDatabaseError for an unretired v1 file."""
    if is_legacy_v1(db_path):
        raise LegacyDatabaseError(
            f"{db_path} is a v1 prototype database. Run `python -m auction_agent cleanup-legacy` "
            "(or any crawl, which retires it automatically) first."
        )
    with connect(db_path) as conn:
        migrate(conn)
        yield conn


def check_identity_key(conn: sqlite3.Connection, current_key_id: str) -> None:
    """Refuse to mix hashes made with different keys."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'hmac_key_id'").fetchone()
    if row is None:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('hmac_key_id', ?)", (current_key_id,)
            )
    elif row["value"] != current_key_id:
        raise RuntimeError(
            "The identity HMAC key changed since this database was created. Restore the "
            "previous key (data/identity.key or IDENTITY_HMAC_KEY) or start a new database."
        )


# --------------------------------------------------------------------------- repository

_STUB_FIELDS = ("brand", "model", "trim", "fuel", "year", "mileage_km", "gearbox", "storage_location")


def upsert_sale(conn: sqlite3.Connection, sale: Any, now: str) -> int:
    row = conn.execute(
        """
        INSERT INTO sales (house, external_id, kind, audience, room, title, url,
                           starts_at, ends_at, expected_lots, first_seen_at, last_listed_at)
        VALUES (:house, :external_id, :kind, :audience, :room, :title, :url,
                :starts_at, :ends_at, :expected_lots, :now, :now)
        ON CONFLICT (house, external_id) DO UPDATE SET
            kind = excluded.kind,
            audience = excluded.audience,
            room = COALESCE(excluded.room, room),
            title = COALESCE(excluded.title, title),
            url = excluded.url,
            starts_at = COALESCE(excluded.starts_at, starts_at),
            ends_at = COALESCE(excluded.ends_at, ends_at),
            expected_lots = COALESCE(excluded.expected_lots, expected_lots),
            last_listed_at = excluded.last_listed_at
        RETURNING id
        """,
        {
            "house": sale.house,
            "external_id": sale.external_id,
            "kind": sale.kind,
            "audience": sale.audience,
            "room": sale.room,
            "title": sale.title,
            "url": sale.url,
            "starts_at": utc_iso(sale.starts_at) if sale.starts_at else None,
            "ends_at": utc_iso(sale.ends_at) if sale.ends_at else None,
            "expected_lots": sale.expected_lots,
            "now": now,
        },
    ).fetchone()
    return int(row["id"])


def apply_snapshot(
    conn: sqlite3.Connection, house: str, sale_id: int, stubs: Sequence[Any], now: str
) -> dict[str, int]:
    """Set-based apply of one sale's listing stubs. Call inside write_tx().

    Every stub gets a vehicle row and a listing row (new ones included). Lifecycle
    columns are reset for every vehicle seen. Missing-marking is not done here: it
    belongs to complete, unscoped snapshots (Phase 2).
    """
    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS snap (
            external_id TEXT NOT NULL, category TEXT NOT NULL, lot_number INTEGER,
            detail_url TEXT NOT NULL, starting_price REAL, current_price_attr REAL,
            closing_hint_at TEXT, brand TEXT, model TEXT, trim TEXT, fuel TEXT, year INTEGER,
            mileage_km INTEGER, gearbox TEXT, storage_location TEXT,
            PRIMARY KEY (external_id)
        ) WITHOUT ROWID
        """
    )
    conn.execute("DELETE FROM temp.snap")
    conn.executemany(
        """
        INSERT OR IGNORE INTO temp.snap VALUES (
            :external_id, :category, :lot_number, :detail_url, :starting_price,
            :current_price_attr, :closing_hint_at, :brand, :model, :trim, :fuel, :year,
            :mileage_km, :gearbox, :storage_location)
        """,
        [
            {
                "external_id": s.external_id,
                "category": s.category,
                "lot_number": s.lot_number,
                "detail_url": s.detail_url,
                "starting_price": s.starting_price,
                "current_price_attr": s.current_price_attr,
                "closing_hint_at": utc_iso(s.closing_hint_at) if s.closing_hint_at else None,
                "brand": s.brand,
                "model": s.model,
                "trim": s.trim,
                "fuel": s.fuel,
                "year": s.year,
                "mileage_km": s.mileage_km,
                "gearbox": s.gearbox,
                "storage_location": s.storage_location,
            }
            for s in stubs
        ],
    )
    params = {"h": house, "sale_id": sale_id, "now": now}
    new_count = conn.execute(
        """SELECT count(*) FROM temp.snap s WHERE NOT EXISTS
           (SELECT 1 FROM vehicles v WHERE v.house = :h AND v.external_id = s.external_id)""",
        params,
    ).fetchone()[0]
    relist_count = conn.execute(
        """SELECT count(*) FROM temp.snap s JOIN vehicles v
             ON v.house = :h AND v.external_id = s.external_id
           WHERE NOT EXISTS (SELECT 1 FROM listings l WHERE l.vehicle_id = v.id AND l.sale_id = :sale_id)
             AND EXISTS (SELECT 1 FROM listings l WHERE l.vehicle_id = v.id)""",
        params,
    ).fetchone()[0]
    # Listing-card fields only fill gaps: detail-page values take precedence.
    conn.execute(
        """
        INSERT INTO vehicles (house, external_id, category, brand, model, trim, fuel, year,
                              mileage_km, gearbox, storage_location, first_seen_at, last_seen_at)
        SELECT :h, external_id, category, brand, model, trim, fuel, year, mileage_km, gearbox,
               storage_location, :now, :now
        FROM temp.snap WHERE true
        ON CONFLICT (house, external_id) DO UPDATE SET
            category = CASE WHEN vehicles.category = 'other' THEN excluded.category ELSE vehicles.category END,
            brand = COALESCE(vehicles.brand, excluded.brand),
            model = COALESCE(vehicles.model, excluded.model),
            trim = COALESCE(vehicles.trim, excluded.trim),
            fuel = COALESCE(vehicles.fuel, excluded.fuel),
            year = COALESCE(vehicles.year, excluded.year),
            mileage_km = COALESCE(vehicles.mileage_km, excluded.mileage_km),
            gearbox = COALESCE(vehicles.gearbox, excluded.gearbox),
            storage_location = COALESCE(excluded.storage_location, vehicles.storage_location),
            last_seen_at = excluded.last_seen_at,
            missed_snapshots = 0,
            missing_since = NULL,
            lifecycle = 'active'
        """,
        params,
    )
    conn.execute(
        """
        INSERT INTO listings (vehicle_id, sale_id, lot_number, detail_url, starting_price,
                              current_price_attr, closing_hint_at, first_seen_at, last_seen_at)
        SELECT v.id, :sale_id, s.lot_number, s.detail_url, s.starting_price, s.current_price_attr,
               s.closing_hint_at, :now, :now
        FROM temp.snap s JOIN vehicles v ON v.house = :h AND v.external_id = s.external_id
        WHERE true
        ON CONFLICT (vehicle_id, sale_id) DO UPDATE SET
            lot_number = COALESCE(excluded.lot_number, lot_number),
            detail_url = excluded.detail_url,
            starting_price = COALESCE(excluded.starting_price, starting_price),
            current_price_attr = COALESCE(excluded.current_price_attr, current_price_attr),
            closing_hint_at = COALESCE(excluded.closing_hint_at, closing_hint_at),
            last_seen_at = excluded.last_seen_at
        """,
        params,
    )
    total = conn.execute("SELECT count(*) FROM temp.snap").fetchone()[0]
    return {"lots_seen": total, "new": new_count, "relisted": relist_count}


_DETAIL_FIELDS = (
    "brand", "model", "trim", "fuel", "gearbox", "body_type", "first_registration", "year",
    "mileage_km", "engine_cc", "power_hp", "co2", "battery_soh_pct", "battery_leased",
    "warranty", "photo_count", "storage_location", "vin_hmac",
)


def update_vehicle_detail(
    conn: sqlite3.Connection, vehicle_id: int, fields: dict[str, Any]
) -> None:
    """COALESCE-update detail fields: a missing value never erases a stored one."""
    sets, params = [], {"id": vehicle_id}
    for key in _DETAIL_FIELDS:
        if key in fields:
            sets.append(f"{key} = COALESCE(:{key}, {key})")
            params[key] = fields[key]
    for key in ("vat_regime",):
        if fields.get(key) and fields[key] != "unknown":
            sets.append(f"{key} = :{key}")
            params[key] = fields[key]
    # Comments and raw specs replace the previous version when a new one was read.
    for key in ("comments", "comments_sha256", "raw_specs_json"):
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = :{key}")
            params[key] = fields[key]
    if not sets:
        return
    conn.execute(f"UPDATE vehicles SET {', '.join(sets)} WHERE id = :id", params)


def mark_complete(
    conn: sqlite3.Connection, vehicle_id: int, *, ct_state: Optional[str], docs_ok: bool = True
) -> None:
    """Successful ingest: clears the retry state. ``ct_state`` None keeps the stored value."""
    conn.execute(
        """UPDATE vehicles SET ingest_state = 'complete', ingest_attempts = 0, next_attempt_at = NULL,
               last_error = NULL, ct_state = COALESCE(:ct_state, ct_state),
               doc_attempts = CASE WHEN :docs_ok THEN 0 ELSE doc_attempts + 1 END
           WHERE id = :id""",
        {"id": vehicle_id, "ct_state": ct_state, "docs_ok": 1 if docs_ok else 0},
    )


def mark_detail_fetched(conn: sqlite3.Connection, vehicle_id: int, now: str, *, late_ct_check: bool = False) -> None:
    """Record a successful detail fetch without touching the ingest retry state."""
    conn.execute(
        """UPDATE vehicles SET detail_fetched_at = :now,
               late_ct_checked_at = CASE WHEN :late THEN :now ELSE late_ct_checked_at END
           WHERE id = :id""",
        {"id": vehicle_id, "now": now, "late": 1 if late_ct_check else 0},
    )


def record_failure(
    conn: sqlite3.Connection, vehicle_id: int, *, now_dt: datetime, error: str, set_failed: bool
) -> int:
    """Count one failed attempt and schedule the next one (2^n hours, max 24 h).

    ``set_failed`` marks a not-yet-ingested car as failed; a complete car keeps its
    state and only records the error. Returns the new attempt count.
    """
    row = conn.execute(
        """UPDATE vehicles SET
               ingest_state = CASE WHEN :set_failed THEN 'failed' ELSE ingest_state END,
               ingest_attempts = ingest_attempts + 1,
               last_error = :error
           WHERE id = :id RETURNING ingest_attempts""",
        {"id": vehicle_id, "error": error[:500], "set_failed": 1 if set_failed else 0},
    ).fetchone()
    attempts = int(row["ingest_attempts"]) if row else 1
    nxt = utc_iso(now_dt + timedelta(hours=min(24, 2 ** attempts)))
    conn.execute("UPDATE vehicles SET next_attempt_at = ? WHERE id = ?", (nxt, vehicle_id))
    return attempts


def upsert_document(conn: sqlite3.Connection, vehicle_id: int, doc: dict[str, Any], now: str) -> None:
    conn.execute(
        """
        INSERT INTO documents (vehicle_id, doc_type, source_url, sha256, size_bytes, fetch_status,
                               parse_status, parser_version, parse_confidence, text, parsed_json,
                               fetched_at)
        VALUES (:vehicle_id, :doc_type, :source_url, :sha256, :size_bytes, :fetch_status,
                :parse_status, :parser_version, :parse_confidence, :text, :parsed_json, :now)
        ON CONFLICT (vehicle_id, doc_type) DO UPDATE SET
            source_url = excluded.source_url,
            sha256 = excluded.sha256,
            size_bytes = excluded.size_bytes,
            fetch_status = excluded.fetch_status,
            parse_status = excluded.parse_status,
            parser_version = excluded.parser_version,
            parse_confidence = excluded.parse_confidence,
            text = excluded.text,
            parsed_json = excluded.parsed_json,
            fetched_at = excluded.fetched_at
        """,
        {
            "vehicle_id": vehicle_id,
            "doc_type": doc["doc_type"],
            "source_url": doc["source_url"],
            "sha256": doc.get("sha256"),
            "size_bytes": doc.get("size_bytes"),
            "fetch_status": doc["fetch_status"],
            "parse_status": doc.get("parse_status"),
            "parser_version": doc.get("parser_version"),
            "parse_confidence": doc.get("parse_confidence"),
            "text": doc.get("text"),
            "parsed_json": json.dumps(doc["parsed"], ensure_ascii=False) if doc.get("parsed") else None,
            "now": now,
        },
    )


def touch_document_source(conn: sqlite3.Connection, vehicle_id: int, doc_type: str, source_url: str) -> None:
    conn.execute(
        "UPDATE documents SET source_url = ? WHERE vehicle_id = ? AND doc_type = ?",
        (source_url, vehicle_id, doc_type),
    )


def get_document(conn: sqlite3.Connection, vehicle_id: int, doc_type: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE vehicle_id = ? AND doc_type = ?", (vehicle_id, doc_type)
    ).fetchone()


def vehicles_for_external_ids(
    conn: sqlite3.Connection, house: str, external_ids: Iterable[str]
) -> dict[str, sqlite3.Row]:
    ids = list(external_ids)
    out: dict[str, sqlite3.Row] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT * FROM vehicles WHERE house = ? AND external_id IN ({marks})", [house, *chunk]
        ):
            out[row["external_id"]] = row
    return out


# --------------------------------------------------------------------------- run lock


def acquire_run(
    conn: sqlite3.Connection,
    house: str,
    kind: str,
    scope: dict[str, Any],
    now: str,
    stale_before: str,
    force: bool = False,
) -> int:
    """Take the (house, kind) run lock, taking over a stale one.

    A run is stale when its heartbeat is older than ``stale_before`` (or ``force``).
    """
    with write_tx(conn):
        if force:
            conn.execute(
                """UPDATE sync_runs SET status = 'failed', finished_at = :now,
                       error = 'lock released with --force-unlock'
                   WHERE house = :h AND kind = :k AND status = 'running'""",
                {"h": house, "k": kind, "now": now},
            )
        conn.execute(
            """UPDATE sync_runs SET status = 'failed', finished_at = :now,
                   error = 'stale lock taken over'
               WHERE house = :h AND kind = :k AND status = 'running'
                 AND COALESCE(heartbeat_at, started_at) < :stale""",
            {"h": house, "k": kind, "now": now, "stale": stale_before},
        )
        try:
            row = conn.execute(
                """INSERT INTO sync_runs (house, kind, status, scope_json, started_at, heartbeat_at)
                   VALUES (?, ?, 'running', ?, ?, ?) RETURNING id""",
                (house, kind, json.dumps(scope, ensure_ascii=False), now, now),
            ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise RunLockedError(
                f"Another {kind!r} run for {house!r} is in progress. If it crashed, wait 15 "
                "minutes or rerun with --force-unlock."
            ) from exc
    return int(row["id"])


def heartbeat(conn: sqlite3.Connection, run_id: int, now: str) -> bool:
    cur = conn.execute(
        "UPDATE sync_runs SET heartbeat_at = ? WHERE id = ? AND status = 'running'", (now, run_id)
    )
    return cur.rowcount == 1


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    now: str,
    counts: Optional[dict[str, Any]] = None,
    notes: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> bool:
    """Close the run. Returns False if another run had taken the lock over (fenced)."""
    counts = counts or {}
    cur = conn.execute(
        """UPDATE sync_runs SET status = :status, finished_at = :now, lots_seen = :lots_seen,
               expected_lots = :expected_lots, new_count = :new, relist_count = :relisted,
               detailed_count = :detailed, documents_count = :documents, failed_count = :failed,
               notes_json = :notes, error = :error
           WHERE id = :id AND status = 'running'""",
        {
            "status": status,
            "now": now,
            "id": run_id,
            "lots_seen": counts.get("lots_seen"),
            "expected_lots": counts.get("expected_lots"),
            "new": counts.get("new"),
            "relisted": counts.get("relisted"),
            "detailed": counts.get("detailed"),
            "documents": counts.get("documents"),
            "failed": counts.get("failed"),
            "notes": json.dumps(notes, ensure_ascii=False) if notes else None,
            "error": error,
        },
    )
    return cur.rowcount == 1


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "sales": q("SELECT count(*) FROM sales"),
        "vehicles": q("SELECT count(*) FROM vehicles"),
        "vehicles_complete": q("SELECT count(*) FROM vehicles WHERE ingest_state = 'complete'"),
        "vehicles_failed": q("SELECT count(*) FROM vehicles WHERE ingest_state = 'failed'"),
        "documents": q("SELECT count(*) FROM documents WHERE fetch_status = 'ok'"),
        "assessments": q("SELECT count(*) FROM assessments"),
        "green": q("SELECT count(*) FROM assessments WHERE tier = 'green'"),
        "yellow": q("SELECT count(*) FROM assessments WHERE tier = 'yellow'"),
        "red": q("SELECT count(*) FROM assessments WHERE tier = 'red'"),
        "llm_cached": q("SELECT count(*) FROM llm_assessments WHERE status = 'ok'"),
        "last_run": dict(conn.execute(
            "SELECT house, kind, status, started_at, finished_at FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone() or {}),
        "db_pages": q("PRAGMA page_count"),
        "db_free_pages": q("PRAGMA freelist_count"),
    }
