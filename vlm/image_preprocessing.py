# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image preparation helpers for VLM requests."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)


def prepare_image_for_vlm(
    image_path: str,
    *,
    max_long_edge: int = 1600,
    output_format: str = "png",
    jpeg_quality: int = 90,
) -> Tuple[str, bool, Dict[str, Any]]:
    """Return an image path sized for VLM use.

    Oversized images are resized to keep request payloads bounded. PNG is the
    default because many PDF images are charts, tables, screenshots, or formulas
    where text readability matters more than byte-level compression.

    Returns:
        (path_to_use, is_temporary, metadata)
    """
    src = Path(image_path)
    if max_long_edge <= 0 or not src.exists():
        return image_path, False, {}

    fmt = output_format.lower().lstrip(".")
    if fmt not in {"png", "jpg", "jpeg"}:
        fmt = "png"

    try:
        with Image.open(src) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            long_edge = max(width, height)
            if long_edge <= max_long_edge:
                return image_path, False, {
                    "original_size": (width, height),
                    "prepared_size": (width, height),
                    "resized": False,
                }

            scale = max_long_edge / long_edge
            new_size = (
                max(1, round(width * scale)),
                max(1, round(height * scale)),
            )
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS
            prepared = image.resize(new_size, resample)

            suffix = ".jpg" if fmt in {"jpg", "jpeg"} else ".png"
            tmp = tempfile.NamedTemporaryFile(
                suffix=suffix,
                prefix="vlm_prepared_",
                delete=False,
            )
            tmp.close()

            save_kwargs: Dict[str, Any]
            if fmt in {"jpg", "jpeg"}:
                prepared = _flatten_to_rgb(prepared)
                save_kwargs = {
                    "format": "JPEG",
                    "quality": max(1, min(int(jpeg_quality), 100)),
                    "optimize": True,
                }
            else:
                save_kwargs = {"format": "PNG", "optimize": True}

            prepared.save(tmp.name, **save_kwargs)
            logger.debug(
                "Prepared VLM image %s: %sx%s -> %sx%s",
                image_path,
                width,
                height,
                new_size[0],
                new_size[1],
            )
            return tmp.name, True, {
                "original_size": (width, height),
                "prepared_size": new_size,
                "resized": True,
            }
    except Exception as exc:
        logger.warning("Failed to prepare VLM image %s: %s", image_path, exc)
        return image_path, False, {}


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        background = Image.new("RGB", image.convert("RGBA").size, "white")
        background.paste(image.convert("RGBA"), mask=image.convert("RGBA").split()[-1])
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image
