# Recruiter Outreach API

Simple Python API to send personalized recruiter emails via Gmail SMTP, attach a resume, and store recruiter records in SQLite.

## Features
- `POST /send` endpoint
- Template placeholders: `{firstName}`, `{ROLE}`, `{Company}`
- Subject defaults to `Immediate Joiner | {ROLE} | Java, Spring Boot, Kafka`
- Optional per-request experience segment in subject, like `2.5+`
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
- `EMAIL_TEMPLATE_PATH` (defaults to `email_template.html`)
- Optional: `HOST`, `PORT`, `DRY_RUN`, `DB_PATH`

## Run
```bash
python3 outreach_api.py
```

By default it runs on:
- `http://127.0.0.1:8000`

## API Request
`POST /send`

Body (array of objects):
```json
[
  {
    "role": "Backend Engineer",
    "company": "Atlassian",
    "domain": "atlassian.com",
    "recruiters": [
      "John Doe",
      "samreen@atlassian.com",
      {
        "name": "Riya Verma",
        "email": "riya@atlassian.com"
      }
    ],
    "include_experience_in_subject": false,
    "subject_experience": "2.5+"
  }
]
```

`domain` is optional. If omitted, generated emails use `firstname.lastname@company.com`.
If a recruiter entry looks like an email address, or a recruiter object includes `email`, that email is used directly instead of generating one from the name.
If `include_experience_in_subject` is `false`, the subject becomes `Immediate Joiner | Backend Engineer | Java, Spring Boot, Kafka`.
If `include_experience_in_subject` is `true`, you must also send `subject_experience`, and the subject becomes `Immediate Joiner | Backend Engineer with 2.5+ YOE | Java, Spring Boot, Kafka`.

## Stop Server
Press `Ctrl + C` in the running terminal.
