# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read secrets from Podman/Docker secret mounts with environment variable fallback.

In production (Podman rootless), sensitive values like API keys are mounted
as files at /run/secrets/<NAME> via quadlet Secret= directives.  In dev,
the same values come from environment variables or .env files.

Usage:
    from utils.secrets import get_secret
    api_key = get_secret("OPENROUTER_API_KEY")
"""

import os
from pathlib import Path

SECRETS_DIR = Path("/run/secrets")


def get_secret(name: str, default: str = "") -> str:
    """Return a secret value, checking Podman secret mount first, then env var.

    Resolution order:
    1. /run/secrets/<name>  (Podman/Docker secret mount)
    2. os.environ[<name>]   (environment variable / .env)
    3. default
    """
    secret_path = SECRETS_DIR / name
    if secret_path.is_file():
        try:
            return secret_path.read_text().strip()
        except OSError:
            pass
    return os.environ.get(name, default)
