# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ollama VLM client for image understanding and text generation.

Uses Ollama's native /api/chat endpoint which supports vision models
with base64-encoded images.
"""

import base64
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from vlm.base_client import BaseVLMClient

logger = logging.getLogger(__name__)


class OllamaClient(BaseVLMClient):
    """Client for interacting with an Ollama server."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5vl:7b",
        temperature: float = 0.7,
        max_tokens: int = 1000,
        timeout: int = 120,
        num_ctx: Optional[int] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.num_ctx = num_ctx

        # Persistent session for HTTP connection pooling (reuses TCP)
        self.session = requests.Session()

    @classmethod
    def from_config(cls, llm_config: Dict[str, Any]) -> "OllamaClient":
        """Create an OllamaClient from config.yaml llm section."""
        return cls(
            base_url=llm_config.get("base_url", "http://localhost:11434"),
            model=llm_config.get("model", "qwen2.5vl:7b"),
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 1000),
            num_ctx=llm_config.get("num_ctx"),
        )

    def check_connection(self) -> bool:
        """Check if the Ollama server is reachable."""
        try:
            resp = self.session.get(f"{self.base_url}/api/tags", timeout=10)
            return resp.status_code == 200
        except requests.ConnectionError:
            return False

    def check_model(self) -> bool:
        """Check if the configured model is available on the server."""
        try:
            resp = self.session.get(f"{self.base_url}/api/tags", timeout=10)
            if resp.status_code != 200:
                return False
            models = resp.json().get("models", [])
            model_names = [m.get("name", "") for m in models]
            # Check exact match or prefix match
            return self.model in model_names or any(
                name.startswith(self.model.split(":")[0]) for name in model_names
            )
        except Exception:
            return False

    def generate(
        self,
        prompt: str,
        images: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate a response from the VLM.

        Args:
            prompt: The user prompt text.
            images: List of file paths to images to include.
            system_prompt: Optional system prompt.

        Returns:
            The model's text response.

        Raises:
            ConnectionError: If the server is unreachable.
            RuntimeError: If the API returns an error.
        """
        messages = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Build user message with optional images
        user_message: Dict[str, Any] = {"role": "user", "content": prompt}

        if images:
            encoded_images = []
            for img_path in images:
                img_data = self._encode_image(img_path)
                if img_data:
                    encoded_images.append(img_data)
            if encoded_images:
                user_message["images"] = encoded_images

        messages.append(user_message)

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
                **({"num_ctx": self.num_ctx} if self.num_ctx else {}),
            },
        }

        start_time = time.time()
        logger.debug(f"Sending request to {self.base_url}/api/chat (model: {self.model})")

        try:
            resp = self.session.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            )
        except requests.ConnectionError as e:
            raise ConnectionError(
                f"Cannot reach Ollama server at {self.base_url}: {e}"
            ) from e
        except requests.Timeout as e:
            raise RuntimeError(
                f"Ollama request timed out after {self.timeout}s: {e}"
            ) from e

        elapsed = time.time() - start_time

        if resp.status_code != 200:
            raise RuntimeError(
                f"Ollama API error {resp.status_code}: {resp.text}"
            )

        result = resp.json()
        content = result.get("message", {}).get("content", "")

        logger.debug(f"VLM response ({elapsed:.1f}s): {content[:100]}...")
        return content

    def _encode_image(self, image_path: str) -> Optional[str]:
        """Read and base64-encode an image file."""
        path = Path(image_path)
        if not path.exists():
            logger.warning(f"Image not found: {image_path}")
            return None

        try:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.warning(f"Failed to encode image {image_path}: {e}")
            return None
