#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Download the Docling models ReadReady uses, so a container works offline.

Constructing DocumentConverter is not enough: Docling loads models lazily,
so each pipeline is initialised explicitly.
"""
import sys

from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.layout_model_specs import (
    DOCLING_LAYOUT_EGRET_LARGE,
    DOCLING_LAYOUT_EGRET_XLARGE,
    DOCLING_LAYOUT_HERON,
)
from docling.datamodel.pipeline_options import LayoutOptions, PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

# Every layout model conversion.layout_model can select (heron is the code
# default, the shipped config.yaml uses egret_xlarge). The image runs with the
# hub offline, so a model missing here cannot be loaded in the container.
LAYOUT_SPECS = (
    DOCLING_LAYOUT_HERON,
    DOCLING_LAYOUT_EGRET_LARGE,
    DOCLING_LAYOUT_EGRET_XLARGE,
)


def main() -> int:
    for spec in LAYOUT_SPECS:
        opts = PdfPipelineOptions()
        opts.do_ocr = False
        opts.layout_options = LayoutOptions(model_spec=spec)
        conv = DocumentConverter(format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=opts,
                                             backend=PyPdfiumDocumentBackend)})
        conv.initialize_pipeline(InputFormat.PDF)
        print(f"cached: {spec.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
