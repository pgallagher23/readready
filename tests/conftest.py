# SPDX-License-Identifier: AGPL-3.0-or-later
# batch_regression_test.py is a CLI harness (argparse/__main__); its
# test_document(pdf_path, ...) is a library function, not a pytest test.
collect_ignore = ["batch_regression_test.py"]

from pathlib import Path

import pytest

_TERMS_FILE = Path(__file__).resolve().parent.parent / "scripts" / "blocked_terms.txt"


@pytest.fixture
def private_terms():
    """Lower-cased terms from the maintainer-only, git-ignored blocked_terms.txt.

    Empty in a public checkout, so branding-absence checks run only where the list exists.
    """
    if not _TERMS_FILE.is_file():
        return []
    lines = _TERMS_FILE.read_text().splitlines()
    return [l.strip().lower() for l in lines if l.strip() and not l.startswith("#")]
