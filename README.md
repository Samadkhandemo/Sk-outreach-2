# Recruiter Outreach API

Simple Python API to send personalized recruiter emails via Gmail SMTP, attach a resume, and store recruiter records in SQLite.

## Features
- `POST /send` endpoint
- Optional scheduling with `schedule_date` in `YYYY-MM-DD` format
- Template placeholders: `{firstName}`, `{ROLE}`, `{Company}`, `{EXPERIENCE}`, `{Personalize text}`
- Subject defaults to `Immediate Joiner | {ROLE} | Java, Spring Boot, Kafka`
- Optional per-request experience segment in subject, like `2.5+`
- Optional per-request `personalized_text` for company-specific content in mail body
- Optional per-request `job_link` to mention where you saw the job post
- Optional per-company `domain` override for email generation
- Resume attachment from `RESUME_PATH`
- Stores `recruiter_name`, `company`, `email` in `outreach.db`
- Duplicate DB rows prevented with a unique index

## Setup
1. Copy env template:
```bash
cp .env.example .env
```
2. Fill values in `.env`:
- `GMAIL_SENDER_EMAIL`
- `GMAIL_APP_PASSWORD`
- `RESUME_PATH`
- `EMAIL_TEMPLATE_PATH` (defaults to `email_template_2.html`)
- Optional: `HOST`, `PORT`, `DRY_RUN`, `DB_PATH`, `SCHEDULER_POLL_INTERVAL_SECONDS`

## Run
```bash
python3 outreach_api.py
```

By default it runs on:
- `http://127.0.0.1:8000`

Scheduled emails are checked automatically while the server is running. By default, pending scheduled emails are scanned every 60 seconds.

## Render Deploy
Use the repo root `Dockerfile` for a Render Web Service.

Set these environment variables in Render:
- `GMAIL_SENDER_EMAIL`
- `GMAIL_APP_PASSWORD`
- `RESUME_PATH`
- `EMAIL_TEMPLATE_PATH` if you want a template other than `email_template_2.html`
- `DB_PATH` if you want a custom SQLite path
- `DRY_RUN=false` when you are ready to send real emails

Render will provide `PORT`; the app binds to `0.0.0.0` automatically inside the container.
If you want SQLite data and scheduled emails to survive restarts, attach a Render persistent disk and set `DB_PATH` to a path on that disk.

## API Request
`POST /send`

Body (array of objects):
```json
[
  {
    "role": "Backend Engineer",
    "company": "Atlassian",
    "domain": "atlassian.com",
    "schedule_date": "2026-04-06",
    "personalized_text": "I noticed Atlassian's focus on scalable cloud products and strong engineering culture. My recent backend work on high-throughput services with Java, Spring Boot, and Kafka aligns closely with this.",
    "job_link": "https://careers.atlassian.com/jobs/12345",
    "recruiters": [
      "John Doe",
      "samreen@atlassian.com",
      {
        "name": "Riya Verma",
        "first_name": "Riya",
        "email": "riya@atlassian.com"
      }
    ],
    "include_experience_in_subject": false,
    "subject_experience": "2.5+"
  }
]
```

If `schedule_date` is omitted or set to today, emails are processed immediately.
If `schedule_date` is a future date, emails are stored in SQLite and sent automatically on that date by the background scheduler.
If today is Saturday `2026-04-04` and you want Monday delivery, use `2026-04-06`.
Past dates are rejected.
If `personalized_text` is provided, it will replace `{Personalize text}` in the email template.
If `job_link` (or `jobLink`) is provided, the template line `{JobLinkText}` is replaced with a sentence including that link.
If `job_link` is omitted or blank, no job-link sentence is added and no extra blank line is left in the mail body.
If `subject_experience` is provided, it is also used for `{EXPERIENCE}` in the body template (fallback is `2.5+`).
For recruiter objects, you can pass `first_name` (or `mention_name`) to control greeting text even when email format is unusual.

Example:
```json
{
  "name": "Ruchi Butola",
  "first_name": "Ruchi",
  "email": "ruchibutola@fico.com"
}
```

`domain` is optional. If omitted, generated emails use `firstname.lastname@company.com`.
If a recruiter entry looks like an email address, or a recruiter object includes `email`, that email is used directly instead of generating one from the name.
If `include_experience_in_subject` is `false`, the subject becomes `Immediate Joiner | Backend Engineer | Java, Spring Boot, Kafka`.
If `include_experience_in_subject` is `true`, you must also send `subject_experience`, and the subject becomes `Immediate Joiner | Backend Engineer with 2.5+ YOE | Java, Spring Boot, Kafka`.

## API Helpers
- `GET /entries` returns stored sent recruiter/company-handle entries from SQLite
- `GET /applications` returns the grouped application payload JSON
- `GET /applications/refresh` rebuilds the grouped application payload from SQLite
- `GET /scheduled` returns scheduled email entries and their status

## Stop Server
Press `Ctrl + C` in the running terminal.
