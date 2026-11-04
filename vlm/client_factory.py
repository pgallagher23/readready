# SPDX-License-Identifier: AGPL-3.0-or-later
"""VLM client factory — creates the right client based on config.yaml.

Usage:
    from vlm.client_factory import create_vlm_client
    client = create_vlm_client(llm_config)
    client = create_vlm_client(llm_config, text_only=True)  # for text-model tasks
"""

import logging
from typing import Any, Dict

from vlm.base_client import BaseVLMClient

logger = logging.getLogger(__name__)


def create_vlm_client(
    llm_config: Dict[str, Any],
    text_only: bool = False,
) -> BaseVLMClient:
    """Create a VLM client based on the provider field in config.

    Args:
        llm_config: The llm section from config.yaml.
        text_only: If True, use the text-only model variant (for table
            structure analysis and similar non-vision tasks). Uses
            text_model/openai_text_model instead of model/openai_model.

    Returns:
        A BaseVLMClient instance ready to use.
    """
    provider = llm_config.get("provider", "ollama")

    if provider == "anthropic":
        return _create_anthropic_client(llm_config, text_only)
    elif provider == "openai":
        return _create_openai_client(llm_config, text_only)
    else:
        return _create_ollama_client(llm_config, text_only)


def _create_anthropic_client(
    llm_config: Dict[str, Any],
    text_only: bool,
) -> BaseVLMClient:
    from vlm.anthropic_client import AnthropicClient

    if text_only:
        text_config = dict(llm_config)
        text_model = llm_config.get("anthropic_text_model") or llm_config.get("anthropic_model", "claude-haiku-4-5-20251001")
        text_config["anthropic_model"] = text_model
        text_config["temperature"] = 0.3
        text_config["max_tokens"] = 800
        return AnthropicClient.from_config(text_config)

    return AnthropicClient.from_config(llm_config)


def _create_ollama_client(
    llm_config: Dict[str, Any],
    text_only: bool,
) -> BaseVLMClient:
    from vlm.ollama_client import OllamaClient

    if text_only:
        model = llm_config.get("text_model") or llm_config.get("model", "llama3.2:latest")
        return OllamaClient(
            base_url=llm_config.get("base_url", "http://localhost:11434"),
            model=model,
            temperature=0.3,
            max_tokens=800,
            timeout=llm_config.get("table_fix_timeout", 180),
            num_ctx=llm_config.get("num_ctx"),
        )

    return OllamaClient.from_config(llm_config)


def _create_openai_client(
    llm_config: Dict[str, Any],
    text_only: bool,
) -> BaseVLMClient:
    from vlm.openai_client import OpenAICompatibleClient

    if text_only:
        # Override model to text variant, lower temperature
        text_config = dict(llm_config)
        text_model = llm_config.get("openai_text_model") or llm_config.get("openai_model", "google/gemini-2.0-flash-001")
        text_config["openai_model"] = text_model
        text_config["temperature"] = 0.3
        text_config["max_tokens"] = 800
        return OpenAICompatibleClient.from_config(text_config)

    return OpenAICompatibleClient.from_config(llm_config)
