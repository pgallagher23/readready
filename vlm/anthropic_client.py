# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native Anthropic Messages API client for VLM tasks.

Uses the official anthropic Python SDK for direct API access, bypassing
OpenRouter.  Supports vision (base64 images) and text-only generation.
The SDK handles 429 rate-limit retries with exponential backoff automatically.
"""

import base64
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic

from utils.secrets import get_secret
from vlm.base_client import BaseVLMClient

logger = logging.getLogger(__name__)

# Anthropic vision API supports these media types.
_SUPPORTED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class AnthropicClient(BaseVLMClient):
    """Client for the Anthropic Messages API."""

    def __init__(
        self,
        api_key: str = "",
        model: str = "claude-haiku-4-5-20251001",
        temperature: float = 0.7,
        max_tokens: int = 1000,
        timeout: int = 120,
    ):
        self.base_url = "https://api.anthropic.com"
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

        self.client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout,
            max_retries=3,
        )

    @classmethod
    def from_config(cls, llm_config: Dict[str, Any]) -> "AnthropicClient":
        """Create client from config.yaml llm section.

        API key resolution order:
        1. llm_config["api_key"]
        2. ANTHROPIC_API_KEY env var / Podman secret
        """
        api_key = (
            llm_config.get("api_key")
            or get_secret("ANTHROPIC_API_KEY")
            or ""
        )
        return cls(
            api_key=api_key,
            model=llm_config.get("anthropic_model", "claude-haiku-4-5-20251001"),
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 1000),
            timeout=llm_config.get("timeout", 120),
        )

    def check_connection(self) -> bool:
        """Check if an API key is configured."""
        if not self.client.api_key:
            logger.warning("No API key configured for Anthropic client")
            return False
        return True

    def generate(
        self,
        prompt: str,
        images: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate a response via the Anthropic Messages API."""
        content: list[dict[str, Any]] = []

        if images:
            for img_path in images:
                block = self._encode_image_block(img_path)
                if block:
                    content.append(block)

        content.append({"type": "text", "text": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": content}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        start_time = time.time()
        logger.debug("Sending request to Anthropic API (model: %s)", self.model)

        try:
            response = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise RuntimeError(f"Anthropic authentication failed: {e}") from e
        except anthropic.APIConnectionError as e:
            raise ConnectionError(f"Cannot reach Anthropic API: {e}") from e
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Anthropic API error {e.status_code}: {e.message}") from e

        elapsed = time.time() - start_time

        text = response.content[0].text if response.content else ""
        logger.debug("Anthropic response (%.1fs): %s...", elapsed, text[:100])
        return text

    def _encode_image_block(self, image_path: str) -> Optional[Dict[str, Any]]:
        """Read and base64-encode an image as an Anthropic image content block."""
        path = Path(image_path)
        if not path.exists():
            logger.warning("Image not found: %s", image_path)
            return None

        media_type = mimetypes.guess_type(str(path))[0] or "image/png"
        if media_type not in _SUPPORTED_MEDIA_TYPES:
            logger.warning("Unsupported media type %s for %s, using image/png", media_type, image_path)
            media_type = "image/png"

        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            }
        except Exception as e:
            logger.warning("Failed to encode image %s: %s", image_path, e)
            return None
