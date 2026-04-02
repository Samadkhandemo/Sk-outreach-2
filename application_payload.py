from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_ROLE = "Backend Engineer"
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "converter" / "appliesOutput.json"
logger = logging.getLogger(__name__)


def fetch_recruiters(db_path: Path) -> list[dict[str, Any]]:
    logger.info("Loading recruiter entries from DB: %s", db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, recruiter_name, company, email, created_at
            FROM recruiter_outreach
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        entries = [dict(row) for row in rows]
        logger.info("Loaded %s recruiter entries from DB", len(entries))
        return entries
    finally:
        conn.close()


def build_application_payload(
    records: list[dict[str, Any]],
    role: str,
    send_company_handles: bool = False,
) -> list[dict[str, Any]]:
    company_map: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"recruiters": [], "domain": ""}
    )

    for record in records:
        company = str(record.get("company", "")).strip()
        recruiter = str(record.get("recruiter_name", "")).strip()
        email = str(record.get("email", "")).strip().lower()
        if not company or not recruiter or "@" not in email:
            continue

        domain = email.split("@", 1)[1]
        company_entry = company_map[company]
        if not company_entry["domain"]:
            company_entry["domain"] = domain
        if recruiter not in company_entry["recruiters"]:
            company_entry["recruiters"].append(recruiter)

    result: list[dict[str, Any]] = []
    for company, info in company_map.items():
        result.append(
            {
                "role": role,
                "company": company,
                "domain": info["domain"],
                "send_company_handles": send_company_handles,
                "recruiters": info["recruiters"],
            }
        )
    return result


def refresh_application_payload(
    db_path: Path,
    role: str = DEFAULT_ROLE,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    send_company_handles: bool = False,
) -> list[dict[str, Any]]:
    logger.info(
        "Refreshing application payload from DB=%s into output=%s with role=%s",
        db_path,
        output_path,
        role,
    )
    applications = build_application_payload(
        fetch_recruiters(db_path),
        role=role,
        send_company_handles=send_company_handles,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(applications, indent=2), encoding="utf-8")
    logger.info("Application payload refreshed with %s grouped entries", len(applications))
    return applications
