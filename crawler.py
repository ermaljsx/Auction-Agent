"""Main crawler pipeline orchestrating listing, details, and documents."""
import time
from typing import Optional

from rich.console import Console

from config import DEFAULT_SALE_URL, REQUEST_DELAY_SECONDS
from database import (
    init_db,
    insert_document,
    insert_photos,
    upsert_sale,
    upsert_vehicle,
    get_stats,
)
from pdf_extractor import extract_text_from_pdf
from scraper.detail_scraper import scrape_vehicle_detail
from scraper.document_downloader import download_vehicle_documents
from scraper.listing_scraper import get_session, scrape_all_listings

console = Console()


def run_full_crawl(
    sale_url: str = DEFAULT_SALE_URL,
    max_pages: Optional[int] = None,
    limit_vehicles: Optional[int] = None,
    download_docs: bool = True,
) -> dict:
    """
    Execute the end-to-end crawl pipeline:
    1. Initialize database schema
    2. Crawl paginated listings to discover all vehicles
    3. Crawl detail page for each vehicle
    4. Download documents (CT, maintenance, battery)
    5. Extract text from PDFs
    6. Return summary stats
    """
    console.rule("[bold blue]Alcopa Auction Crawler[/bold blue]")

    # 1. Database init
    console.print("[dim]Initializing database...[/dim]")
    init_db()

    # 2. Scrape listings
    sale_metadata, vehicles_summary = scrape_all_listings(
        sale_url=sale_url,
        max_pages=max_pages,
    )

    if not vehicles_summary:
        console.print("[red]No vehicles found. Aborting.[/red]")
        return {}

    # Save sale to DB
    sale_id = upsert_sale(
        sale_room=sale_metadata.get("sale_room", "Beauvais"),
        sale_date=sale_metadata.get("sale_date", ""),
        sale_title=sale_metadata.get("sale_title", ""),
        sale_url=sale_url,
        total_lots=sale_metadata.get("total_lots", len(vehicles_summary)),
    )
    console.print(f"[green]Saved sale ID: {sale_id}[/green]")

    # Limit vehicles if specified (for testing)
    target_vehicles = vehicles_summary[:limit_vehicles] if limit_vehicles else vehicles_summary
    console.print(f"[bold]Processing {len(target_vehicles)} vehicles...[/bold]")

    session = get_session()

    # 3. Process each vehicle
    total = len(target_vehicles)
    for idx, v_summary in enumerate(target_vehicles, start=1):
        detail_url = v_summary.get("detail_url")
        lot_number = v_summary.get("lot_number")
        brand = v_summary.get("brand", "Unknown")
        model = v_summary.get("model", "")
        console.print(f"[{idx}/{total}] Scraping Lot {lot_number}: {brand} {model}...")

        # Scrape detail page
        detail_data = scrape_vehicle_detail(detail_url, session)

        # Merge listing summary with detail data (detail overrides summary)
        merged = {**v_summary, **{k: v for k, v in detail_data.items() if v is not None}}

        # Save vehicle to DB
        vehicle_id = upsert_vehicle(
            sale_id=sale_id,
            **merged,
        )

        # Save photos
        photos = detail_data.get("photos", [])
        if photos:
            insert_photos(vehicle_id, photos)

        # Download documents & extract text
        if download_docs:
            docs = detail_data.get("documents", [])
            if docs:
                downloaded_docs = download_vehicle_documents(
                    lot_number=lot_number,
                    vehicle_id=vehicle_id,
                    documents=docs,
                    session=session,
                )

                for doc in downloaded_docs:
                    local_path = doc.get("local_path")
                    extracted_text = None

                    if local_path:
                        extracted_text = extract_text_from_pdf(local_path)

                    insert_document(
                        vehicle_id=vehicle_id,
                        doc_type=doc.get("type", "unknown"),
                        doc_type_label=doc.get("label", ""),
                        download_url=doc.get("url", ""),
                        local_path=local_path,
                        extracted_text=extracted_text,
                    )

        time.sleep(REQUEST_DELAY_SECONDS)

    # Print summary
    stats = get_stats()
    console.rule("[bold green]Crawl Summary[/bold green]")
    console.print(f"  Sales:     {stats['sales']}")
    console.print(f"  Vehicles:  {stats['vehicles']}")
    console.print(f"  Documents: {stats['documents']}")
    console.print(f"  Photos:    {stats['photos']}")
    console.print(f"  Brands:    {stats['brands']}")

    return stats
