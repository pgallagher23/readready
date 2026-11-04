# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 1.0.0 — unreleased

Initial public extraction of ReadReady.

- Pipeline that converts untagged or poorly tagged PDFs into tagged
  documents while keeping the original layout (Docling conversion, text and OCR
  recovery, WCAG audit, VLM descriptions, HTML remediation, iText tagger).
- Command-line tool (single files or directories) and Gradio web UI.
- Configurable branding through the `branding:` section of `config.yaml`.
- `READREADY_CONFIG` and `READREADY_FONT_DIR` environment variables; font import
  script (`make fonts`).
- Multi-stage Dockerfile, Docker Compose stack, and GitHub Actions workflow.
- AGPL-3.0-or-later license with a NOTICE for third-party components.

### Notes for this release

- There is no automatic PII gate. With a hosted provider configured, the
  images, tables, and formulas of every document are sent to that provider;
  the README says how to keep processing local.
- Owner-password restrictions (no copy, no print) are removed from the input
  so it can be tagged; the output PDF has no restrictions. PDFs that need a
  password to open are rejected.
- `READREADY_SHARED_DIR` sets the directory shared with Celery workers
  (default `/app/output`).
