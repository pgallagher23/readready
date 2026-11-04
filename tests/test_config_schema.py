# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path

import pytest
import yaml

from utils.config_schema import validate_config, ConfigError, REQUIRED_SECTIONS

BASE = {s: {} for s in REQUIRED_SECTIONS}


def test_minimal_config_has_no_warnings():
    assert validate_config(dict(BASE)) == []


def test_unknown_top_level_key_warns_with_suggestion():
    w = validate_config({**BASE, "remediaton": {}})
    assert len(w) == 1 and "remediaton" in w[0] and "remediation" in w[0]


def test_missing_required_section_raises():
    cfg = dict(BASE)
    del cfg["llm"]
    with pytest.raises(ConfigError, match="llm"):
        validate_config(cfg)


def test_bad_branding_color_raises():
    with pytest.raises(ConfigError, match="branding.colors.primary"):
        validate_config({**BASE, "branding": {"colors": {"primary": "red"}}})


def test_absent_branding_is_fine():
    assert validate_config(dict(BASE)) == []


def test_llm_num_ctx_must_be_int():
    with pytest.raises(ConfigError, match="llm.num_ctx"):
        validate_config({**BASE, "llm": {"num_ctx": "8k"}})


def test_shipped_config_yaml_validates_cleanly():
    path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(path) as f:
        cfg = yaml.safe_load(f)
    assert validate_config(cfg) == []
