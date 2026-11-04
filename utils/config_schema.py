# SPDX-License-Identifier: AGPL-3.0-or-later
"""Light validation of config.yaml: required sections, unknown keys, typed branding/llm."""
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from utils.branding import branding_from_config, BrandingError

REQUIRED_SECTIONS = ("llm", "conversion", "audit", "remediation", "output")
KNOWN_SECTIONS = REQUIRED_SECTIONS + (
    "verdict", "ocr_quality", "formulas", "table_analysis",
    "ui", "editor", "resources", "concurrency", "queue", "branding",
)


class ConfigError(ValueError):
    pass


class LLM(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5vl:7b"
    num_ctx: Optional[int] = None
    max_parallel: int = 8


def _check_model(model: type, section: str, data: Any) -> None:
    try:
        model(**(data or {}))
    except ValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(x) for x in err["loc"])
        raise ConfigError(f"config.yaml: {section}.{loc}: {err['msg']}") from e
    except TypeError as e:  # section is not a mapping
        raise ConfigError(f"config.yaml: section '{section}' must be a mapping") from e


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """Validate a loaded config dict; return warnings, raise ConfigError on fatal problems."""
    warnings: List[str] = []
    for s in REQUIRED_SECTIONS:
        if s not in cfg:
            raise ConfigError(f"config.yaml: required section '{s}' is missing")
    for key in cfg:
        if key not in KNOWN_SECTIONS:
            hint = difflib.get_close_matches(key, KNOWN_SECTIONS, n=1)
            warnings.append(
                f"config.yaml: unknown section '{key}'"
                + (f" (did you mean '{hint[0]}'?)" if hint else "")
            )
    _check_model(LLM, "llm", cfg.get("llm"))
    try:
        branding_from_config(cfg.get("branding"))
    except BrandingError as e:
        raise ConfigError(f"config.yaml: {e}") from e
    return warnings
