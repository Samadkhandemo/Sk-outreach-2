#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from smtplib import SMTP_SSL
from typing import Any

from application_payload import fetch_recruiters, refresh_application_payload


EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


@dataclass
class EmailTask:
    recruiter_name: str
    role: str
    company: str
    domain: str | None = None
    recipient_type: str = "recruiter"
    recipient_email: str | None = None
    include_experience_in_subject: bool = False
    subject_experience: str = ""

    @property
    def first_name(self) -> str:
        cleaned = self.recruiter_name.strip()
        if not cleaned:
            return ""
        if "@" in cleaned and EMAIL_REGEX.match(cleaned.lower()):
            inferred = infer_name_from_email(cleaned)
            if inferred:
                return inferred.split()[0]
        return cleaned.split()[0]


def load_env_file(path: Path) -> None:
    if not path.exists():
        logger.info("Env file not found at %s, using existing environment", path)
        return
    logger.info("Loading environment variables from %s", path)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_path(raw: str, base_dir: Path = BASE_DIR) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def normalize_company_domain(company: str) -> str:
    domain = company.strip().lower()
    domain = re.sub(r"[^a-z0-9]+", "", domain)
    return domain


def normalize_input_domain(domain: str) -> str:
    cleaned = domain.strip().lower()
    cleaned = re.sub(r"^https?://", "", cleaned)
    cleaned = cleaned.split("/", 1)[0]
    cleaned = re.sub(r"^www\.", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9.-]", "", cleaned)
    cleaned = re.sub(r"\.+", ".", cleaned).strip(".")
    if cleaned and "." not in cleaned:
        cleaned = f"{cleaned}.com"
    return cleaned


def resolve_domain(company: str, domain: str | None = None) -> str:
    return normalize_input_domain(domain) if domain else f"{normalize_company_domain(company)}.com"


def infer_name_from_email(email: str) -> str:
    local_part = email.split("@", 1)[0].strip().lower()
    normalized = re.sub(r"[._-]+", " ", local_part)
    normalized = re.sub(r"\d+", " ", normalized).strip()
    return normalized.title() if normalized else email


def generate_email(name: str, company: str, domain: str | None = None) -> str:
    parts = [part for part in name.strip().lower().split() if part]
    if not parts:
        raise ValueError("Recruiter name is empty, cannot generate email.")
    first = re.sub(r"[^a-z0-9]", "", parts[0])
    last = re.sub(r"[^a-z0-9]", "", parts[-1]) if len(parts) > 1 else ""
    company_domain = resolve_domain(company, domain)
    if not first or not company_domain:
        raise ValueError(
            f"Cannot generate email for name='{name}', company='{company}', domain='{domain}'."
        )
    local = f"{first}.{last}" if last else first
    return f"{local}@{company_domain}"


def init_db(db_path: Path) -> None:
    logger.info("Initializing database at %s", db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recruiter_outreach (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recruiter_name TEXT NOT NULL,
                company TEXT NOT NULL,
                email TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            DELETE FROM recruiter_outreach
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM recruiter_outreach
                GROUP BY recruiter_name, company, email
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_recruiter_outreach_identity
            ON recruiter_outreach(recruiter_name, company, email)
            """
        )
        conn.commit()
        logger.info("Database initialization completed for %s", db_path)
    finally:
        conn.close()


def save_recruiter(db_path: Path, recruiter_name: str, company: str, email: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO recruiter_outreach (recruiter_name, company, email)
            VALUES (?, ?, ?)
            """,
            (recruiter_name, company, email),
        )
        conn.commit()
        inserted = cursor.rowcount > 0
        if inserted:
            logger.info("Stored recruiter entry for %s at %s", recruiter_name, email)
        else:
            logger.info("Skipped duplicate recruiter entry for %s at %s", recruiter_name, email)
        return inserted
    finally:
        conn.close()


def render_html_template(template: str, task: EmailTask) -> str:
    first_name = task.first_name if task.recipient_type == "recruiter" else "Team"
    rendered = (
        template.replace("{firstName}", first_name)
        .replace("{ROLE}", task.role)
        .replace("{Company}", task.company)
    )
    return rendered.replace("\r\n", "\n").replace("\n", "<br>\n")


def strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


def create_message(
    sender_email: str,
    to_email: str,
    subject: str,
    html_body: str,
    resume_path: Path,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(strip_html(html_body))
    msg.add_alternative(html_body, subtype="html")

    with resume_path.open("rb") as f:
        data = f.read()
    msg.add_attachment(
        data,
        maintype="application",
        subtype="octet-stream",
        filename=resume_path.name,
    )
    return msg


def parse_bool_field(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n", ""}:
            return False
    return bool(value)


def build_email_subject(task: EmailTask) -> str:
    role_segment = task.role
    if task.include_experience_in_subject and task.subject_experience:
        role_segment = f"{task.role} with {task.subject_experience} YOE"
    subject_parts = ["Immediate Joiner", role_segment]
    subject_parts.append("Java, Spring Boot, Kafka")
    return " | ".join(subject_parts)


def build_recruiter_task(
    recruiter_item: Any,
    *,
    item_index: int,
    recruiter_index: int,
    role: str,
    company: str,
    resolved_domain: str,
    include_experience_in_subject: bool,
    subject_experience: str,
) -> EmailTask | None:
    if isinstance(recruiter_item, str):
        raw_value = recruiter_item.strip()
        if EMAIL_REGEX.match(raw_value):
            recruiter_name = infer_name_from_email(raw_value)
            recruiter_email = raw_value.lower()
            logger.info(
                "Detected recruiter email directly in payload for company=%s email=%s",
                company,
                recruiter_email,
            )
        else:
            recruiter_name = raw_value
            try:
                recruiter_email = generate_email(recruiter_name, company, resolved_domain)
            except ValueError:
                recruiter_email = None
    elif isinstance(recruiter_item, dict):
        explicit_email = str(recruiter_item.get("email", "")).strip().lower() or None
        recruiter_name = str(
            recruiter_item.get(
                "name",
                recruiter_item.get(
                    "recruiter_name",
                    infer_name_from_email(explicit_email) if explicit_email else "",
                ),
            )
        ).strip()
        if explicit_email:
            recruiter_email = explicit_email
            logger.info(
                "Using recruiter email provided in object for recruiter=%s company=%s email=%s",
                recruiter_name or explicit_email,
                company,
                recruiter_email,
            )
        else:
            try:
                recruiter_email = generate_email(recruiter_name, company, resolved_domain)
            except ValueError:
                recruiter_email = None
    else:
        raise ValueError(
            f"Item {item_index} recruiter {recruiter_index} must be a string or object"
        )

    if not recruiter_name:
        return None

    return EmailTask(
        recruiter_name=recruiter_name,
        role=role,
        company=company,
        domain=resolved_domain,
        recipient_type="recruiter",
        recipient_email=recruiter_email,
        include_experience_in_subject=include_experience_in_subject,
        subject_experience=subject_experience,
    )


class OutreachHandler(BaseHTTPRequestHandler):
    sender_email = ""
    app_password = ""
    resume_path = Path("")
    db_path = Path("outreach.db")
    template = ""
    dry_run = False
    default_role = "Backend Engineer"
    application_payload: list[dict[str, Any]] = []
    application_output_path = BASE_DIR / "converter" / "appliesOutput.json"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info(
            "HTTP access client=%s path=%s %s",
            self.client_address[0],
            self.path,
            format % args,
        )

    def do_GET(self) -> None:
        logger.info("Handling GET request for %s", self.path)
        if self.path == "/applications":
            self._json(
                200,
                {
                    "count": len(self.application_payload),
                    "applications": self.application_payload,
                    "db_path": str(self.db_path),
                    "role": self.default_role,
                },
            )
            return

        if self.path == "/applications/refresh":
            try:
                self.application_payload = refresh_application_payload(
                    self.db_path,
                    role=self.default_role,
                    output_path=self.application_output_path,
                )
            except Exception as exc:
                logger.exception("Application payload refresh failed")
                self._json(500, {"error": str(exc)})
                return

            logger.info(
                "Application payload refreshed via API with %s grouped entries",
                len(self.application_payload),
            )
            self._json(
                200,
                {
                    "count": len(self.application_payload),
                    "applications": self.application_payload,
                    "db_path": str(self.db_path),
                    "output_path": str(self.application_output_path),
                    "role": self.default_role,
                    "refreshed": True,
                },
            )
            return

        if self.path != "/entries":
            self._json(404, {"error": "Not found"})
            return

        try:
            entries = fetch_recruiters(self.db_path)
        except Exception as exc:
            logger.exception("Failed to fetch recruiter entries")
            self._json(500, {"error": str(exc)})
            return

        self._json(
            200,
            {
                "count": len(entries),
                "entries": entries,
                "db_path": str(self.db_path),
            },
        )

    def do_POST(self) -> None:
        logger.info("Handling POST request for %s", self.path)
        if self.path != "/send":
            self._json(404, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "Invalid Content-Length"})
            return
        if length <= 0:
            self._json(400, {"error": "Request body required"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"error": "Invalid JSON"})
            return

        if not isinstance(payload, list):
            self._json(400, {"error": "Payload must be an array of objects"})
            return

        logger.info("Received send request with %s top-level items", len(payload))

        tasks: list[EmailTask] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                self._json(400, {"error": f"Item {index} must be an object"})
                return
            role = str(item.get("role", "")).strip()
            company = str(item.get("company", "")).strip()
            domain = str(item.get("domain", "")).strip() or None
            send_company_handles = bool(item.get("send_company_handles", False))
            include_experience_in_subject = parse_bool_field(
                item.get("include_experience_in_subject", False)
            )
            subject_experience = str(item.get("subject_experience", "")).strip()
            recruiters = item.get("recruiters")
            if not role or not company or not isinstance(recruiters, list) or not recruiters:
                self._json(
                    400,
                    {
                        "error": (
                            f"Item {index} requires non-empty role, company, "
                            "and recruiters array"
                        )
                    },
                )
                return
            if include_experience_in_subject and not subject_experience:
                self._json(
                    400,
                    {
                        "error": (
                            f"Item {index} must provide non-empty subject_experience when "
                            "include_experience_in_subject is true"
                        )
                    },
                )
                return

            resolved_domain = resolve_domain(company, domain)
            if not resolved_domain:
                self._json(
                    400,
                    {"error": f"Item {index} has invalid company/domain for email generation"},
                )
                return

            item_tasks: list[EmailTask] = []
            for recruiter_index, recruiter_item in enumerate(recruiters):
                try:
                    task = build_recruiter_task(
                        recruiter_item,
                        item_index=index,
                        recruiter_index=recruiter_index,
                        role=role,
                        company=company,
                        resolved_domain=resolved_domain,
                        include_experience_in_subject=include_experience_in_subject,
                        subject_experience=subject_experience,
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                if task is not None:
                    item_tasks.append(task)

            if send_company_handles:
                item_tasks.append(
                    EmailTask(
                        recruiter_name="Careers Team",
                        role=role,
                        company=company,
                        domain=resolved_domain,
                        recipient_type="company_handle",
                        recipient_email=f"careers@{resolved_domain}",
                        include_experience_in_subject=include_experience_in_subject,
                        subject_experience=subject_experience,
                    )
                )
                item_tasks.append(
                    EmailTask(
                        recruiter_name="Talent Acquisition Team",
                        role=role,
                        company=company,
                        domain=resolved_domain,
                        recipient_type="company_handle",
                        recipient_email=f"talentacquisition@{resolved_domain}",
                        include_experience_in_subject=include_experience_in_subject,
                        subject_experience=subject_experience,
                    )
                )
                item_tasks.append(
                    EmailTask(
                        recruiter_name="HR Team",
                        role=role,
                        company=company,
                        domain=resolved_domain,
                        recipient_type="company_handle",
                        recipient_email=f"hr@{resolved_domain}",
                        include_experience_in_subject=include_experience_in_subject,
                        subject_experience=subject_experience,
                    )
                )

            seen_emails: set[str] = set()
            for task in item_tasks:
                if not task.recipient_email:
                    tasks.append(task)
                    continue
                email_key = task.recipient_email.lower()
                if email_key in seen_emails:
                    continue
                seen_emails.add(email_key)
                tasks.append(task)

        if not tasks:
            self._json(400, {"error": "No valid recruiters in payload"})
            return

        logger.info("Prepared %s email tasks for processing", len(tasks))

        sent = 0
        stored = 0
        duplicate_skipped = 0
        recruiter_attempted = 0
        recruiter_sent = 0
        recruiter_stored = 0
        recruiter_duplicate_skipped = 0
        company_handle_attempted = 0
        company_handle_sent = 0
        company_handle_stored = 0
        company_handle_duplicate_skipped = 0
        skipped: list[dict[str, str]] = []
        smtp = None
        try:
            if not self.dry_run:
                context = ssl.create_default_context()
                smtp = SMTP_SSL("smtp.gmail.com", 465, context=context)
                smtp.login(self.sender_email, self.app_password)
                logger.info("SMTP connection established for sender %s", self.sender_email)
            else:
                logger.info("Running send request in dry-run mode")

            for task in tasks:
                if task.recipient_type == "recruiter":
                    recruiter_attempted += 1
                else:
                    company_handle_attempted += 1

                to_email = task.recipient_email
                if not to_email:
                    skipped.append(
                        {
                            "recipient": task.recruiter_name,
                            "recipient_type": task.recipient_type,
                            "reason": "Could not generate email",
                        }
                    )
                    continue

                if not EMAIL_REGEX.match(to_email):
                    skipped.append(
                        {
                            "recipient": task.recruiter_name,
                            "recipient_type": task.recipient_type,
                            "reason": f"Generated invalid email: {to_email}",
                        }
                    )
                    continue

                html_body = render_html_template(self.template, task)
                subject = build_email_subject(task)

                if not self.dry_run:
                    msg = create_message(
                        sender_email=self.sender_email,
                        to_email=to_email,
                        subject=subject,
                        html_body=html_body,
                        resume_path=self.resume_path,
                    )
                    smtp.send_message(msg)
                    logger.info(
                        "Email sent to %s for company=%s recipient_type=%s",
                        to_email,
                        task.company,
                        task.recipient_type,
                    )

                inserted = save_recruiter(
                    self.db_path,
                    task.recruiter_name,
                    task.company,
                    to_email,
                )
                if inserted:
                    stored += 1
                    if task.recipient_type == "recruiter":
                        recruiter_stored += 1
                    else:
                        company_handle_stored += 1
                else:
                    duplicate_skipped += 1
                    if task.recipient_type == "recruiter":
                        recruiter_duplicate_skipped += 1
                    else:
                        company_handle_duplicate_skipped += 1
                sent += 1
                if task.recipient_type == "recruiter":
                    recruiter_sent += 1
                else:
                    company_handle_sent += 1
        except Exception as exc:
            logger.exception("Send request failed")
            self._json(500, {"error": str(exc)})
            return
        finally:
            if smtp is not None:
                smtp.quit()
                logger.info("SMTP connection closed")

        logger.info(
            "Send request completed processed=%s sent=%s stored=%s skipped=%s duplicates=%s",
            len(tasks),
            sent,
            stored,
            len(skipped),
            duplicate_skipped,
        )

        self._json(
            200,
            {
                "processed": len(tasks),
                "sent": sent,
                "stored": stored,
                "duplicate_skipped": duplicate_skipped,
                "skipped_count": len(skipped),
                "skipped": skipped,
                "dry_run": self.dry_run,
                "db_path": str(self.db_path),
                "counters_by_type": {
                    "recruiter": {
                        "attempted": recruiter_attempted,
                        "sent": recruiter_sent,
                        "stored": recruiter_stored,
                        "duplicate_skipped": recruiter_duplicate_skipped,
                    },
                    "company_handle": {
                        "attempted": company_handle_attempted,
                        "sent": company_handle_sent,
                        "stored": company_handle_stored,
                        "duplicate_skipped": company_handle_duplicate_skipped,
                    },
                },
            },
        )


def build_handler() -> type[OutreachHandler]:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env_file(BASE_DIR / ".env")

    sender_email = os.getenv("GMAIL_SENDER_EMAIL", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    resume_path = resolve_path(os.getenv("RESUME_PATH", "").strip())
    db_path = resolve_path(os.getenv("DB_PATH", "outreach.db"))
    dry_run = os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"}
    default_role = os.getenv("DEFAULT_ROLE", "Backend Engineer").strip() or "Backend Engineer"
    application_output_path = resolve_path(
        os.getenv("APPLICATION_OUTPUT_PATH", "converter/appliesOutput.json").strip()
        or "converter/appliesOutput.json"
    )

    template_path_value = os.getenv("EMAIL_TEMPLATE_PATH", "email_template.html").strip()
    template_path = resolve_path(template_path_value)
    if not template_path.exists():
        raise FileNotFoundError(f"EMAIL_TEMPLATE_PATH not found: {template_path}")
    template = template_path.read_text(encoding="utf-8")

    if not sender_email:
        raise ValueError("Missing env var: GMAIL_SENDER_EMAIL")
    if not dry_run and not app_password:
        raise ValueError("Missing env var: GMAIL_APP_PASSWORD")
    if not dry_run and not resume_path.exists():
        raise ValueError("RESUME_PATH not found or missing")

    init_db(db_path)
    application_payload = refresh_application_payload(
        db_path,
        role=default_role,
        output_path=application_output_path,
    )
    logger.info(
        "Startup application payload ready with %s grouped entries at %s",
        len(application_payload),
        application_output_path,
    )

    class ConfiguredOutreachHandler(OutreachHandler):
        pass

    ConfiguredOutreachHandler.sender_email = sender_email
    ConfiguredOutreachHandler.app_password = app_password
    ConfiguredOutreachHandler.resume_path = resume_path
    ConfiguredOutreachHandler.db_path = db_path
    ConfiguredOutreachHandler.template = template
    ConfiguredOutreachHandler.dry_run = dry_run
    ConfiguredOutreachHandler.default_role = default_role
    ConfiguredOutreachHandler.application_payload = application_payload
    ConfiguredOutreachHandler.application_output_path = application_output_path
    return ConfiguredOutreachHandler


def main() -> int:
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
    handler = build_handler()
    server = ThreadingHTTPServer((host, port), handler)
    logger.info("Outreach API starting on http://%s:%s", host, port)
    logger.info("Using DB at %s", handler.db_path)
    print(f"Outreach API listening on http://{host}:{port}")
    print(f"Using DB: {handler.db_path}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
