# Fonts

This directory is the drop-in font directory for ReadReady. The tagger looks
here first for the metric-matched families it embeds (Arial, Times New Roman,
Courier New, Calibri, and others), so output matches the original document's
line widths more closely. The tagger reads these families from the top level
of this directory only.

Files imported from macOS folders are copied into `apple/` unchanged. The
tagger looks there only for TTFs with the exact names it expects, which are
snake_case files extracted from macOS collections (for example
`helvetica_neue.ttf`). The import script does not extract faces from `.ttc`
collections; that is a separate step (fontTools can do it) that you perform
yourself.

To import fonts from a machine you are licensed to use, run:

    python scripts/import_fonts.py SOURCE_DIR [SOURCE_DIR ...]

The script copies `.ttf`, `.otf`, `.ttc` and `.otc` files, skips duplicates and
never overwrites an existing file, writes `manifest.json` here (git-ignored,
because it records absolute local paths), and prints which metric-matched
families are now available at the top level. A later `make fonts SRC=...` target
wraps the same command. Use `--dry-run` to preview, or `--check` to print the
coverage table without importing anything.

ReadReady ships no proprietary fonts. Import only fonts you are licensed to use.
Without them the pipeline falls back to free metric-compatible fonts, and the
output is still fully tagged. See the Fonts section of the
[README](../README.md#fonts) for how the fallbacks work.
