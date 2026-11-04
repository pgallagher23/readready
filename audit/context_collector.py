# SPDX-License-Identifier: AGPL-3.0-or-later
"""Context collector for accessibility issues."""

from typing import Dict, Any
from bs4 import Tag

# Readers use at most the first 160 chars. Uncapped, an issue on <html>/<body>
# carried the whole document's text into every copy of the audit (5MB Celery
# results that filled Redis on 2026-09-29). Matches the html_snippet cap.
MAX_TEXT_CONTENT_CHARS = 500


def _capped_attrs(element) -> Dict[str, Any]:
    """Copy of the element's attrs with long string values capped.

    Docling inlines figures as base64 data: URIs (~140KB each), which every
    image issue otherwise copies into its context.
    """
    attrs = getattr(element, "attrs", None) or {}
    return {
        k: v[:MAX_TEXT_CONTENT_CHARS] if isinstance(v, str) else v
        for k, v in attrs.items()
    }


class ContextCollector:
    """Collects context information around an HTML element."""

    def __init__(self, element: Tag):
        self.element = element

    def collect(self) -> Dict[str, Any]:
        context = {}

        if not self.element or not hasattr(self.element, "name"):
            return {"error": "Invalid element"}

        context["element_name"] = self.element.name
        context["attributes"] = _capped_attrs(self.element)

        try:
            context["text_content"] = self.element.get_text(strip=True)[:MAX_TEXT_CONTENT_CHARS]
        except Exception:
            context["text_content"] = ""

        parent = self.element.parent
        if parent and hasattr(parent, "name"):
            context["parent"] = {
                "element_name": parent.name,
                "attributes": _capped_attrs(parent),
            }

        try:
            context["html_snippet"] = str(self.element)[:500]
        except Exception:
            context["html_snippet"] = ""

        context["position"] = self._get_position()
        return context

    def _get_position(self) -> Dict[str, Any]:
        position = {}
        try:
            if self.element.name:
                all_elements = self.element.find_all_previous(self.element.name) + [
                    self.element
                ]
                position["index"] = len(all_elements) - 1
                position["total"] = len(all_elements) + len(
                    self.element.find_all_next(self.element.name)
                )
        except Exception:
            position["index"] = -1
            position["total"] = -1
        return position
