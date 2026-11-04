# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Celery-mode shared volume path comes from the environment."""
from pathlib import Path

import app


def test_default_is_the_compose_volume():
    assert app._shared_output_root({}) == Path("/app/output")


def test_env_overrides_the_shared_root():
    assert app._shared_output_root({"READREADY_SHARED_DIR": "/srv/rr"}) == Path("/srv/rr")
