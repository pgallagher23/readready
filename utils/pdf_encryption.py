# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovery of owner-password-only ("permissions") encrypted PDFs.

Many PDFs flagged as encrypted carry only an *owner* password (permissions
restrictions) with an empty *user* password — they are fully readable. The
pipeline historically rejected all encrypted PDFs at preflight, discarding
these recoverable documents.

`recover_if_owner_encrypted` decrypts owner-only PDFs to a working copy so the
rest of the pipeline can process them, and reports (via ``None``) the genuinely
user-password-protected PDFs that still cannot be processed.

The decrypted copy keeps the **same basename** as the original (written into a
``decrypted/`` subdir of the work dir) so every downstream consumer that derives
output names / report titles from ``pdf_path.stem`` / ``pdf_path.name`` keeps
working unchanged — only the file content differs.
"""

from pathlib import Path
from typing import Optional, Union

import logging

logger = logging.getLogger(__name__)


def recover_if_owner_encrypted(
    pdf_path: Union[str, Path], work_dir: Union[str, Path]
) -> Optional[Path]:
    """Return a processable path for ``pdf_path``.

    - Not encrypted (or not openable by pypdf): returns the original path
      unchanged (let downstream produce the real error for malformed files).
    - Owner-password-only encryption: saves a decrypted copy under
      ``<work_dir>/decrypted/<original-name>.pdf`` and returns that path.
    - Genuine user-password encryption (or unrecoverable): returns ``None``.
    """
    pdf_path = Path(pdf_path)

    try:
        import pikepdf
    except ImportError:
        return pdf_path  # cannot assess without pikepdf — let downstream try

    # pikepdf is the authority: it opens with an empty user password (so it
    # succeeds for unencrypted AND owner-only PDFs) and raises PasswordError
    # only when a real user password is required.
    try:
        doc = pikepdf.open(str(pdf_path))
    except pikepdf.PasswordError:
        logger.error("PDF requires a user password; cannot process: %s", pdf_path.name)
        return None
    except Exception:
        # Not openable for a non-password reason (malformed / not a PDF) —
        # return the original and let Docling produce the real error.
        return pdf_path

    try:
        if not doc.is_encrypted:
            return pdf_path  # nothing to do

        # Owner-password-only (empty user pw): write a decrypted working copy,
        # keeping the original basename so output naming/reports are unchanged.
        out_dir = Path(work_dir) / "decrypted"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / pdf_path.name
        doc.save(str(out_path))
        logger.info("Recovered owner-password-only PDF: %s", pdf_path.name)
        return out_path
    finally:
        doc.close()
