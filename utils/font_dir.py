# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where user-supplied fonts live. One env var, one default, used everywhere."""
from __future__ import annotations
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_VAR = "READREADY_FONT_DIR"
JVM_PROPERTY = "readready.fontDir"


def resolve_font_dir() -> Path:
    """READREADY_FONT_DIR if set, else <repo>/fonts. Never raises; may not exist."""
    raw = os.environ.get(ENV_VAR, "").strip()
    return Path(raw).expanduser() if raw else REPO_ROOT / "fonts"


def jvm_font_flag() -> str:
    return f"-D{JVM_PROPERTY}={resolve_font_dir()}"
