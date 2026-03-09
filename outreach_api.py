#!/usr/bin/env python3
from __future__ import annotations

import json
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


EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
BASE_DIR = Path(__file__).resolve().parent


@dataclass
class EmailTask:
    recruiter_name: str
    role: str
    company: str
    domain: str | None = None

    @property
    def first_name(self) -> str:
        cleaned = self.recruiter_name.strip()
        if not cleaned:
            return ""
        return cleaned.split()[0]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
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


def generate_email(name: str, company: str, domain: str | None = None) -> str:
    parts = [part for part in name.strip().lower().split() if part]
    if not parts:
        raise ValueError("Recruiter name is empty, cannot generate email.")
    first = re.sub(r"[^a-z0-9]", "", parts[0])
    last = re.sub(r"[^a-z0-9]", "", parts[-1]) if len(parts) > 1 else ""
    company_domain = (
        normalize_input_domain(domain) if domain else f"{normalize_company_domain(company)}.com"
    )
    if not first or not company_domain:
        raise ValueError(
            f"Cannot generate email for name='{name}', company='{company}', domain='{domain}'."
        )
    local = f"{first}.{last}" if last else first
    return f"{local}@{company_domain}"


def init_db(db_path: Path) -> None:
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
        return cursor.rowcount > 0
    finally:
        conn.close()


def render_html_template(template: str, task: EmailTask) -> str:
    rendered = (
        template.replace("{firstName}", task.first_name)
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


class OutreachHandler(BaseHTTPRequestHandler):
    sender_email = ""
    app_password = ""
    resume_path = Path("")
    db_path = Path("outreach.db")
    subject_template = "Application for {ROLE} at {Company}"
    template = ""
    dry_run = False

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
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

        tasks: list[EmailTask] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                self._json(400, {"error": f"Item {index} must be an object"})
                return
            role = str(item.get("role", "")).strip()
            company = str(item.get("company", "")).strip()
            domain = str(item.get("domain", "")).strip() or None
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
            for recruiter_name in recruiters:
                recruiter_name = str(recruiter_name).strip()
                if recruiter_name:
                    tasks.append(
                        EmailTask(
                            recruiter_name=recruiter_name,
                            role=role,
                            company=company,
                            domain=domain,
                        )
                    )

        if not tasks:
            self._json(400, {"error": "No valid recruiters in payload"})
            return

        sent = 0
        stored = 0
        duplicate_skipped = 0
        skipped: list[dict[str, str]] = []
        smtp = None
        try:
            if not self.dry_run:
                context = ssl.create_default_context()
                smtp = SMTP_SSL("smtp.gmail.com", 465, context=context)
                smtp.login(self.sender_email, self.app_password)

            for task in tasks:
                try:
                    to_email = generate_email(
                        task.recruiter_name,
                        task.company,
                        task.domain,
                    )
                except ValueError as exc:
                    skipped.append({"recruiter": task.recruiter_name, "reason": str(exc)})
                    continue

                if not EMAIL_REGEX.match(to_email):
                    skipped.append(
                        {
                            "recruiter": task.recruiter_name,
                            "reason": f"Generated invalid email: {to_email}",
                        }
                    )
                    continue

                html_body = render_html_template(self.template, task)
                subject = (
                    self.subject_template.replace("{ROLE}", task.role).replace(
                        "{Company}", task.company
                    )
                )

                if not self.dry_run:
                    msg = create_message(
                        sender_email=self.sender_email,
                        to_email=to_email,
                        subject=subject,
                        html_body=html_body,
                        resume_path=self.resume_path,
                    )
                    smtp.send_message(msg)

                inserted = save_recruiter(
                    self.db_path,
                    task.recruiter_name,
                    task.company,
                    to_email,
                )
                if inserted:
                    stored += 1
                else:
                    duplicate_skipped += 1
                sent += 1
        except Exception as exc:
            self._json(500, {"error": str(exc)})
            return
        finally:
            if smtp is not None:
                smtp.quit()

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
            },
        )


def build_handler() -> type[OutreachHandler]:
    load_env_file(BASE_DIR / ".env")

    sender_email = os.getenv("GMAIL_SENDER_EMAIL", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    resume_path = resolve_path(os.getenv("RESUME_PATH", "").strip())
    db_path = resolve_path(os.getenv("DB_PATH", "outreach.db"))
    subject_template = os.getenv(
        "EMAIL_SUBJECT_TEMPLATE", "Application for {ROLE} at {Company}"
    ).strip()
    dry_run = os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"}

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

    class ConfiguredOutreachHandler(OutreachHandler):
        pass

    ConfiguredOutreachHandler.sender_email = sender_email
    ConfiguredOutreachHandler.app_password = app_password
    ConfiguredOutreachHandler.resume_path = resume_path
    ConfiguredOutreachHandler.db_path = db_path
    ConfiguredOutreachHandler.subject_template = subject_template
    ConfiguredOutreachHandler.template = template
    ConfiguredOutreachHandler.dry_run = dry_run
    return ConfiguredOutreachHandler


def main() -> int:
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
    handler = build_handler()
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Outreach API listening on http://{host}:{port}")
    print(f"Using DB: {handler.db_path}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
