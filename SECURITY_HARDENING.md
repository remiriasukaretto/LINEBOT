# Security Hardening Checklist

## Implemented in this repository
- Parameterized SQL for user inputs (SQL injection control).
- Strict allow-list for sortable SQL columns and sort order.
- CSRF validation for state-changing requests except LINE webhook callback.
- Session hardening:
  - `HttpOnly` + `SameSite=Lax` + secure cookie support.
  - Session rotation on login (`session.clear()` + new CSRF token).
  - Idle timeout via `SESSION_IDLE_TIMEOUT_SECONDS`.
- Login brute-force control (`LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_SECONDS`).
- Webhook abuse control (`WEBHOOK_RATE_LIMIT_COUNT`, `WEBHOOK_RATE_LIMIT_WINDOW_SECONDS`).
- Host header allow-list (`ALLOWED_HOSTS`) and HTTPS enforcement (`FORCE_HTTPS`).
- Response security headers:
  - Content-Security-Policy
  - X-Frame-Options
  - X-Content-Type-Options
  - Referrer-Policy
  - Permissions-Policy
  - Strict-Transport-Security (HTTPS requests)
- Input validation for reservation type names and message length limits.
- Admin state-transition validation:
  - `call` only from `waiting`
  - `finish` only from `arrived`

## Required operational controls (outside app code)
- Always terminate TLS with valid certificates and disable insecure protocols/ciphers.
- Keep OS, runtime, packages, and managed DB patched.
- Restrict network exposure (firewall/security groups) to required ports only.
- Use secret manager for env vars and rotate secrets periodically.
- Enable DB least privilege (app user should not be superuser).
- Monitor logs for 400/403/429 spikes and incident indicators.
- Add backups + restore drill for database.
- If WAF is used, tune detection rules and verify false positives/negatives.
- Subscribe to vulnerability advisories for Flask, line-bot-sdk, psycopg2, gunicorn.

## Notes
- `ADMIN_PASSWORD` (plain-text) is deprecated; use `ADMIN_PASSWORD_HASH` only.
- `ALLOWED_HOSTS` should include only real hostnames used in production.
