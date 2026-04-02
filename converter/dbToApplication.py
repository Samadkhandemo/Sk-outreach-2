import json
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))

from application_payload import refresh_application_payload

DB_PATH = BASE_DIR.parent / "outreach.db"
OUTPUT_FILE = BASE_DIR / "appliesOutput.json"
ROLE = "Backend Engineer"
logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Starting DB to application conversion")
    applications = refresh_application_payload(DB_PATH, role=ROLE, output_path=OUTPUT_FILE)
    logger.info("Wrote %s grouped application entries to %s", len(applications), OUTPUT_FILE)
    print(f"Converted JSON saved to {OUTPUT_FILE}")
    print(json.dumps({"count": len(applications)}, indent=2))


if __name__ == "__main__":
    main()
