# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenAI-compatible VLM client for OpenRouter, OpenAI, and similar APIs.

Uses the /v1/chat/completions endpoint with vision support (base64 images
in the content array). Compatible with OpenRouter, OpenAI, Azure OpenAI,
Together AI, and any provider implementing the OpenAI chat completions API.
"""

import base64
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from utils.secrets import get_secret
from vlm.base_client import BaseVLMClient

logger = logging.getLogger(__name__)


class OpenAICompatibleClient(BaseVLMClient):
    """Client for OpenAI-compatible VLM endpoints (OpenRouter, etc.)."""

    def __init__(
        self,
        base_url: str = "https://openrouter.ai/api/v1",
        model: str = "google/gemini-2.0-flash-001",
        api_key: str = "",
        temperature: float = 0.7,
        max_tokens: int = 1000,
        timeout: int = 120,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

        # Persistent session for HTTP connection pooling (reuses TCP+TLS)
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        })

    @classmethod
    def from_config(cls, llm_config: Dict[str, Any]) -> "OpenAICompatibleClient":
        """Create client from config.yaml llm section.

        API key resolution order:
        1. llm_config["api_key"]
        2. OPENROUTER_API_KEY env var
        3. OPENAI_API_KEY env var
        """
        api_key = (
            llm_config.get("api_key")
            or get_secret("OPENROUTER_API_KEY")
            or get_secret("OPENAI_API_KEY")
            or ""
        )
        return cls(
            base_url=llm_config.get("openai_base_url", "https://openrouter.ai/api/v1"),
            model=llm_config.get("openai_model", "google/gemini-2.0-flash-001"),
            api_key=api_key,
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 1000),
            timeout=llm_config.get("timeout", 120),
        )

    def check_connection(self) -> bool:
        """Check if the API endpoint is reachable and key is set."""
        if not self.api_key:
            logger.warning("No API key configured for OpenAI-compatible client")
            return False
        try:
            resp = self.session.get(
                f"{self.base_url}/models",
                timeout=10,
            )
            return resp.status_code in (200, 401, 403)
        except requests.ConnectionError:
            return False

    def generate(
        self,
        prompt: str,
        images: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate a response via the OpenAI chat completions API."""
        messages = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Build user message content array
        content: List[Dict[str, Any]] = []

        # Add images as base64 data URLs
        if images:
            for img_path in images:
                data_url = self._encode_image_data_url(img_path)
                if data_url:
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    })

        content.append({"type": "text", "text": prompt})
        messages.append({"role": "user", "content": content})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        start_time = time.time()
        logger.debug("Sending request to %s/chat/completions (model: %s)", self.base_url, self.model)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = self.session.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.ConnectionError as e:
                raise ConnectionError(
                    f"Cannot reach API at {self.base_url}: {e}"
                ) from e
            except requests.Timeout as e:
                raise RuntimeError(
                    f"API request timed out after {self.timeout}s: {e}"
                ) from e

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning("Rate limited (429), retrying in %.1fs (attempt %d/%d)",
                               retry_after, attempt + 1, max_retries)
                time.sleep(retry_after)
                continue

            break

        elapsed = time.time() - start_time

        if resp.status_code != 200:
            raise RuntimeError(
                f"API error {resp.status_code}: {resp.text}"
            )

        result = resp.json()
        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError("API returned no choices")

        text = choices[0].get("message", {}).get("content", "")
        logger.debug("API response (%.1fs): %s...", elapsed, text[:100])
        return text

    def _encode_image_data_url(self, image_path: str) -> Optional[str]:
        """Read and base64-encode an image as a data URL."""
        path = Path(image_path)
        if not path.exists():
            logger.warning("Image not found: %s", image_path)
            return None

        mime_type = mimetypes.guess_type(str(path))[0] or "image/png"

        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            return f"data:{mime_type};base64,{b64}"
        except Exception as e:
            logger.warning("Failed to encode image %s: %s", image_path, e)
            return None
