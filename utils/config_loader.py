# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration loader for the local PDF accessibility pipeline."""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(
    os.environ.get("READREADY_CONFIG")
    or Path(__file__).parent.parent / "config.yaml"
)

_config_cache: Optional[Dict[str, Any]] = None


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load configuration from a YAML file.

    Args:
        config_path: Path to the config file. If None, uses the default
            config.yaml in the project root.

    Returns:
        Dictionary of configuration values.
    """
    global _config_cache

    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r") as f:
        config = yaml.safe_load(f)

    _config_cache = config or {}

    from utils.config_schema import validate_config
    for warning in validate_config(_config_cache):
        logger.warning(warning)
    return _config_cache


def get_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Get the current configuration, loading it if necessary.

    Args:
        config_path: Path to the config file. Only used on first load.

    Returns:
        Dictionary of configuration values.
    """
    global _config_cache
    if _config_cache is None:
        return load_config(config_path)
    return _config_cache


def get_section(section: str, config_path: Optional[str] = None) -> Dict[str, Any]:
    """Get a specific section from the configuration.

    Args:
        section: The config section name (e.g., 'llm', 'conversion').
        config_path: Path to the config file. Only used on first load.

    Returns:
        Dictionary of values for the requested section.
    """
    config = get_config(config_path)
    return config.get(section, {})
