"""SQLite database layer for Alcopa Auction data storage."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import DATA_DIR, DB_PATH


def get_db_connection() -> sqlite3.Connection:
    """Create and return a configured database connection."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    """Initialize the database schema if it doesn't exist."""
    with get_db_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_room TEXT NOT NULL,
            sale_date TEXT NOT NULL,
            sale_title TEXT NOT NULL,
            sale_url TEXT NOT NULL UNIQUE,
            total_lots INTEGER DEFAULT 0,
            crawled_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            lot_number INTEGER,
            brand TEXT,
            model TEXT,
            trim TEXT,
            registration TEXT,
            vin TEXT,
            fuel_type TEXT,
            first_registration_date TEXT,
            year INTEGER,
            mileage_km INTEGER,
            color TEXT,
            gearbox TEXT,
            vehicle_type TEXT,
            body_type TEXT,
            co2 INTEGER,
            engine_cc INTEGER,
            vat_recoverable TEXT,
            starting_price REAL,
            current_bid REAL,
            warranty TEXT,
            battery_health TEXT,
            comments TEXT,
            detail_url TEXT NOT NULL UNIQUE,
            image_url TEXT,
            storage_location TEXT,
            crawled_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            doc_type TEXT NOT NULL,           -- 'ct', 'mh', 'moba'
            doc_type_label TEXT NOT NULL,     -- 'Contrôle technique', etc.
            download_url TEXT NOT NULL,
            local_path TEXT,
            extracted_text TEXT,
            crawled_at TEXT NOT NULL,
            UNIQUE(vehicle_id, doc_type)
        );

        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            photo_url TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_vehicles_sale_id ON vehicles(sale_id);
        CREATE INDEX IF NOT EXISTS idx_vehicles_brand ON vehicles(brand);
        CREATE INDEX IF NOT EXISTS idx_vehicles_price ON vehicles(starting_price);
        CREATE INDEX IF NOT EXISTS idx_vehicles_year ON vehicles(year);
        CREATE INDEX IF NOT EXISTS idx_vehicles_mileage ON vehicles(mileage_km);
        CREATE INDEX IF NOT EXISTS idx_documents_vehicle_id ON documents(vehicle_id);
        CREATE INDEX IF NOT EXISTS idx_photos_vehicle_id ON photos(vehicle_id);
        """)


def upsert_sale(
    sale_room: str,
    sale_date: str,
    sale_title: str,
    sale_url: str,
    total_lots: int = 0,
) -> int:
    """Insert or update a sale record, returning its ID."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO sales (sale_room, sale_date, sale_title, sale_url, total_lots, crawled_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sale_url) DO UPDATE SET
                sale_room=excluded.sale_room,
                sale_date=excluded.sale_date,
                sale_title=excluded.sale_title,
                total_lots=excluded.total_lots,
                crawled_at=excluded.crawled_at
            RETURNING id;
            """,
            (sale_room, sale_date, sale_title, sale_url, total_lots, now),
        )
        row = cursor.fetchone()
        return row["id"]


def upsert_vehicle(
    sale_id: int,
    detail_url: Optional[str] = None,
    **kwargs: Any,
) -> int:
    """Insert or update a vehicle record, returning its ID."""
    now = datetime.now(timezone.utc).isoformat()
    target_url = detail_url or kwargs.get("detail_url")
    if not target_url:
        raise ValueError("detail_url is required")

    fields = {
        "sale_id": sale_id,
        "detail_url": target_url,
        "crawled_at": now,
        "lot_number": kwargs.get("lot_number"),
        "brand": kwargs.get("brand"),
        "model": kwargs.get("model"),
        "trim": kwargs.get("trim"),
        "registration": kwargs.get("registration"),
        "vin": kwargs.get("vin"),
        "fuel_type": kwargs.get("fuel_type"),
        "first_registration_date": kwargs.get("first_registration_date"),
        "year": kwargs.get("year"),
        "mileage_km": kwargs.get("mileage_km"),
        "color": kwargs.get("color"),
        "gearbox": kwargs.get("gearbox"),
        "vehicle_type": kwargs.get("vehicle_type"),
        "body_type": kwargs.get("body_type"),
        "co2": kwargs.get("co2"),
        "engine_cc": kwargs.get("engine_cc"),
        "vat_recoverable": kwargs.get("vat_recoverable"),
        "starting_price": kwargs.get("starting_price"),
        "current_bid": kwargs.get("current_bid"),
        "warranty": kwargs.get("warranty"),
        "battery_health": kwargs.get("battery_health"),
        "comments": kwargs.get("comments"),
        "image_url": kwargs.get("image_url"),
        "storage_location": kwargs.get("storage_location"),
    }

    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    updates = ", ".join(
        f"{k}=excluded.{k}" for k in fields.keys() if k != "detail_url"
    )

    with get_db_connection() as conn:
        cursor = conn.cursor()
        sql = f"""
            INSERT INTO vehicles ({cols})
            VALUES ({placeholders})
            ON CONFLICT(detail_url) DO UPDATE SET
                {updates}
            RETURNING id;
        """
        cursor.execute(sql, list(fields.values()))
        row = cursor.fetchone()
        return row["id"]


def insert_document(
    vehicle_id: int,
    doc_type: str,
    doc_type_label: str,
    download_url: str,
    local_path: Optional[str] = None,
    extracted_text: Optional[str] = None,
) -> int:
    """Insert or update a document record."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO documents (
                vehicle_id, doc_type, doc_type_label,
                download_url, local_path, extracted_text, crawled_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vehicle_id, doc_type) DO UPDATE SET
                local_path=COALESCE(excluded.local_path, documents.local_path),
                extracted_text=COALESCE(excluded.extracted_text, documents.extracted_text),
                crawled_at=excluded.crawled_at
            RETURNING id;
            """,
            (
                vehicle_id,
                doc_type,
                doc_type_label,
                download_url,
                local_path,
                extracted_text,
                now,
            ),
        )
        row = cursor.fetchone()
        return row["id"]


def update_document_text(doc_id: int, extracted_text: str) -> None:
    """Update extracted text for a document."""
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE documents SET extracted_text = ? WHERE id = ?;",
            (extracted_text, doc_id),
        )


def insert_photos(vehicle_id: int, photo_urls: list[str]) -> None:
    """Insert photos for a vehicle, replacing existing ones."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM photos WHERE vehicle_id = ?;", (vehicle_id,))
        conn.executemany(
            """
            INSERT INTO photos (vehicle_id, photo_url, sort_order)
            VALUES (?, ?, ?);
            """,
            [(vehicle_id, url, i) for i, url in enumerate(photo_urls)],
        )


def get_all_vehicles(sale_id: Optional[int] = None) -> list[dict]:
    """Retrieve all vehicles, optionally filtered by sale_id."""
    with get_db_connection() as conn:
        if sale_id:
            cursor = conn.execute(
                "SELECT * FROM vehicles WHERE sale_id = ? ORDER BY lot_number ASC;",
                (sale_id,),
            )
        else:
            cursor = conn.execute("SELECT * FROM vehicles ORDER BY lot_number ASC;")
        return [dict(row) for row in cursor.fetchall()]


def get_all_vehicles_with_documents(
    sale_id: Optional[int] = None,
    max_price: Optional[float] = None,
) -> list[dict]:
    """Retrieve all vehicles with their documents and photo count."""
    with get_db_connection() as conn:
        query = "SELECT * FROM vehicles WHERE 1=1"
        params: list[Any] = []

        if sale_id:
            query += " AND sale_id = ?"
            params.append(sale_id)
        if max_price:
            query += " AND (starting_price <= ? OR current_bid <= ?)"
            params.extend([max_price, max_price])

        query += " ORDER BY lot_number ASC;"

        vehicles = [dict(row) for row in conn.execute(query, params).fetchall()]

        # Attach documents and photos for each vehicle
        for v in vehicles:
            v_id = v["id"]

            # Documents
            docs_rows = conn.execute(
                "SELECT doc_type, doc_type_label, local_path, extracted_text FROM documents WHERE vehicle_id = ?;",
                (v_id,),
            ).fetchall()
            v["documents_parsed"] = {
                row["doc_type"]: {
                    "label": row["doc_type_label"],
                    "local_path": row["local_path"],
                    "extracted_text": row["extracted_text"],
                }
                for row in docs_rows
            }

            # Photo count
            photo_count = conn.execute(
                "SELECT COUNT(*) FROM photos WHERE vehicle_id = ?;",
                (v_id,),
            ).fetchone()[0]
            v["photo_count"] = photo_count

        return vehicles


def get_stats() -> dict:
    """Return quick stats about the crawled data."""
    with get_db_connection() as conn:
        sales_count = conn.execute("SELECT COUNT(*) FROM sales;").fetchone()[0]
        vehicles_count = conn.execute("SELECT COUNT(*) FROM vehicles;").fetchone()[0]
        docs_count = conn.execute("SELECT COUNT(*) FROM documents;").fetchone()[0]
        photos_count = conn.execute("SELECT COUNT(*) FROM photos;").fetchone()[0]
        brands_count = conn.execute(
            "SELECT COUNT(DISTINCT brand) FROM vehicles WHERE brand IS NOT NULL;"
        ).fetchone()[0]
        return {
            "sales": sales_count,
            "vehicles": vehicles_count,
            "documents": docs_count,
            "photos": photos_count,
            "brands": brands_count,
        }
