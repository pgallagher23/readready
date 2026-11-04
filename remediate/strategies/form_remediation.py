# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Form remediation strategies.

Fixes missing form labels, required field indicators, and fieldsets.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def remediate_missing_form_labels(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add labels to form controls that lack them."""
    for control in soup.find_all(["input", "select", "textarea"]):
        control_id = control.get("id")

        # Skip if already has a label
        if control_id:
            if soup.find("label", attrs={"for": control_id}):
                continue

        # Skip hidden or submit/button types
        if control.name == "input" and control.get("type") in ("hidden", "submit", "button", "image"):
            continue

        # Generate ID if needed
        if not control_id:
            control_type = control.name
            input_type = control.get("type", "text") if control_type == "input" else control_type
            siblings = len(list(control.find_previous_siblings(control_type)))
            control_id = f"{input_type}-{siblings + 1}"
            control["id"] = control_id

        # Derive label text
        label_text = "Label"
        if control.get("placeholder"):
            label_text = control["placeholder"]
        elif control.get("name"):
            label_text = " ".join(
                word.capitalize() for word in re.split(r"[_\-]", control["name"])
            )
        elif control.get("type"):
            label_text = control["type"].capitalize()

        label = soup.new_tag("label")
        label["for"] = control_id
        label.string = label_text
        control.insert_before(label)

        return f"Added label '{label_text}' for form control"

    return None


def remediate_missing_required_indicators(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add visual and ARIA indicators for required form fields."""
    for control in soup.find_all(["input", "select", "textarea"]):
        if not (control.get("required") or control.get("aria-required") == "true"):
            continue

        control_id = control.get("id")
        if not control_id:
            continue

        label = soup.find("label", attrs={"for": control_id})
        if not label:
            continue

        if "*" in label.get_text():
            continue

        if label.string:
            label.string = f"{label.string} *"
        else:
            label.append(" *")

        if not control.get("aria-required"):
            control["aria-required"] = "true"

        return "Added required field indicator"

    return None


def remediate_missing_fieldsets(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add fieldset and legend elements to group related form controls."""
    for form in soup.find_all("form"):
        if form.find("fieldset"):
            continue

        # Look for radio/checkbox groups
        radio_groups = {}
        for inp in form.find_all("input", type=["radio", "checkbox"]):
            name = inp.get("name", "")
            if name:
                if name not in radio_groups:
                    radio_groups[name] = []
                radio_groups[name].append(inp)

        added = 0
        for name, inputs in radio_groups.items():
            if len(inputs) < 2:
                continue

            fieldset = soup.new_tag("fieldset")
            legend = soup.new_tag("legend")
            legend.string = " ".join(
                word.capitalize() for word in re.split(r"[_\-]", name)
            )
            fieldset.append(legend)

            # Wrap the group
            parent = inputs[0].parent
            if parent:
                parent.wrap(fieldset)
                added += 1

        if added:
            return f"Added {added} fieldset elements"

    return None
