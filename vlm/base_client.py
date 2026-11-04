# SPDX-License-Identifier: AGPL-3.0-or-later
"""Abstract base class for VLM (Vision Language Model) clients.

All VLM providers (Ollama, OpenAI-compatible/OpenRouter, etc.) implement
this interface so they can be swapped via config.yaml.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseVLMClient(ABC):
    """Abstract VLM client interface."""

    #: Server URL used for connection.  Concrete clients must assign this
    #: in ``__init__``.  Read by error-path call sites in
    #: ``vlm/alt_text_generator.py``, ``vlm/formula_processor.py``, and
    #: ``vlm/table_caption_generator.py`` to build
    #: "Cannot connect to VLM at {base_url}" messages.
    base_url: str = ""

    @classmethod
    @abstractmethod
    def from_config(cls, llm_config: Dict[str, Any]) -> "BaseVLMClient":
        """Create a client instance from config.yaml llm section."""
        ...

    @abstractmethod
    def check_connection(self) -> bool:
        """Check if the VLM server is reachable."""
        ...

    @abstractmethod
    def generate(
        self,
        prompt: str,
        images: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate a response from the VLM.

        Args:
            prompt: The user prompt text.
            images: Optional list of file paths to images.
            system_prompt: Optional system prompt.

        Returns:
            The model's text response.

        Raises:
            ConnectionError: If the server is unreachable.
            RuntimeError: If the API returns an error.
        """
        ...
