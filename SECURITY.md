# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

- Prefer a [GitHub private vulnerability report](../../security/advisories/new) on this repository, or
- contact the maintainers by email (see the imprint of the instance you use, or the address in the repository profile).

Please include: affected version/commit, a description of the issue, steps to
reproduce, and the potential impact. We aim to acknowledge reports within
72 hours and to publish a fix or mitigation as quickly as possible.

## Scope

This is a self-hosted event-organization tool. Security-relevant areas include:

- authentication (passwordless e-mail login codes via django-allauth),
- per-event access control (access password, "known persons" signup),
- file uploads/downloads (event attachments, site logo),
- CSRF/session/security headers, and
- the JSONL audit log (`logs/app.jsonl`) and error-report e-mails.

## Operational notes for hosters

- Always set a strong `DJANGO_SECRET_KEY` (the app refuses to start in
  production mode without one) and a strong `POSTGRES_PASSWORD`.
- Keep `DJANGO_DEBUG=0` in production.
- Run behind a TLS-terminating reverse proxy (the compose file expects Traefik);
  the app enforces HTTPS redirects, secure cookies and HSTS.
- Never commit `.env`, `backups/`, `logs/`, `media/`, `cache/` or `staticfiles/`
  (all covered by `.gitignore`).
