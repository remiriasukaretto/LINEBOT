# LINEBOT

## Setup
1. Copy `.env.example` values into your deployment environment.
2. Generate `ADMIN_PASSWORD_HASH` with Werkzeug `generate_password_hash`.
3. Run app with `gunicorn main:app` (see `Procfile`).

## Security
- Security hardening summary and operational checklist: `SECURITY_HARDENING.md`
