# Branding

ReadReady reads its institution branding from the `branding:` section of
`config.yaml` (see `utils/branding.py`). Everything an institution customizes
lives under this directory.

- **`logo`** - put your logo image in this directory and set `branding.logo` to
  its path relative to `static/branding/` (for example `my-logo.png`). Leave it
  empty for a text-only header. Set `logo_alt` to its alternative text.
- **`landing_page.html`** - set `branding.landing_page` to an HTML fragment to
  replace the generic first screen shown before any file is processed.
- **`colors`** - `primary`, `primary_dark`, `accent`, `neutral`, and `warm_bg`
  as `#RRGGBB` values. Check that text on these backgrounds keeps at least a
  4.5:1 contrast ratio.

Set `READREADY_CONFIG` to point at an alternate config file when you want to
keep your branding outside the repository's `config.yaml`.
