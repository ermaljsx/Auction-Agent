"""Downloads PDF documents (Contrôle technique, maintenance, etc.) for vehicles."""
import time
from pathlib import Path
from typing import Any, Optional

import requests
from rich.console import Console

from config import (
    DOCUMENTS_DIR,
    MAX_RETRIES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

console = Console()

DOC_TYPE_FILENAMES = {
    "ct": "controle_technique.pdf",
    "mh": "historique_entretien.pdf",
    "moba": "certificat_batterie.pdf",
}


def download_document(
    doc_url: str,
    save_path: Path,
    session: requests.Session,
) -> Optional[Path]:
    """
    Download a single document from URL and save to path.
    Returns the path if successful, None otherwise.
    """
    # Skip if already downloaded
    if save_path.exists() and save_path.stat().st_size > 0:
        return save_path

    save_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(
                doc_url,
                timeout=REQUEST_TIMEOUT,
                stream=True,
                headers={"User-Agent": USER_AGENT},
            )

            # Check if valid response
            if resp.status_code == 404:
                return None

            resp.raise_for_status()

            # Verify it's a PDF or binary content
            content_type = resp.headers.get("content-type", "").lower()
            # Some servers return application/octet-stream for PDFs
            if "html" in content_type:
                # Returned an error page instead of PDF
                return None

            # Write file
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            # Ensure file is not empty
            if save_path.stat().st_size > 100:
                return save_path
            else:
                save_path.unlink(missing_ok=True)
                return None

        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                console.print(f"[yellow]Failed to download {doc_url}: {e}[/yellow]")
                return None
            time.sleep(1)

    return None


def download_vehicle_documents(
    lot_number: Optional[int],
    vehicle_id: int,
    documents: list[dict[str, Any]],
    session: requests.Session,
) -> list[dict[str, Any]]:
    """
    Download all documents for a vehicle.
    Returns list of updated doc dicts with 'local_path' added.
    """
    folder_name = f"lot_{lot_number}" if lot_number else f"veh_{vehicle_id}"
    vehicle_doc_dir = DOCUMENTS_DIR / folder_name

    downloaded = []
    for doc in documents:
        doc_type = doc.get("type", "unknown")
        doc_url = doc.get("url")

        if not doc_url:
            continue

        filename = DOC_TYPE_FILENAMES.get(doc_type, f"{doc_type}.pdf")
        target_path = vehicle_doc_dir / filename

        local_path = download_document(doc_url, target_path, session)

        downloaded.append({
            **doc,
            "local_path": str(local_path) if local_path else None,
        })

        time.sleep(REQUEST_DELAY_SECONDS / 2)

    return downloaded
