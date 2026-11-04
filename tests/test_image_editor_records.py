# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests: one editor record per image, never duplicates.

The VLM records images by HTML src (a relative path like
``../images/figure_1.png``); the backfill used to compare that verbatim
against bare basenames, never matched, and appended a duplicate
``needs_manual_alt_text`` record for every image — repeating images in the
Alt Text editor and falsely flipping it into "AI processing was disabled"
mode.
"""

import unittest

from utils.editor_records import ensure_image_editor_records


def _generated_record(src, alt="A chart."):
    return {
        "src": src, "file_path": f"/out/images/{src.split('/')[-1]}",
        "alt_text": alt, "outcome": "generated", "skip_reason": None,
    }


class TestEnsureImageEditorRecords(unittest.TestCase):

    def test_relative_src_does_not_duplicate(self):
        """A VLM record with a relative src must block the backfill append."""
        alt_block = {"image_records": [_generated_record("../images/figure_1.png")]}
        ensure_image_editor_records(
            alt_block, ["/out/images/figure_1.png"], struct_data=None
        )
        records = alt_block["image_records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "generated")

    def test_unprocessed_image_still_gets_a_record(self):
        """Images the VLM never saw must still appear in the editor."""
        alt_block = {"image_records": [_generated_record("../images/figure_1.png")]}
        ensure_image_editor_records(
            alt_block,
            ["/out/images/figure_1.png", "/out/images/figure_2.png"],
            struct_data=None,
        )
        records = alt_block["image_records"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["src"], "figure_2.png")
        self.assertEqual(records[1]["outcome"], "needs_manual_alt_text")

    def test_harvested_alt_matches_relative_src(self):
        """Source-PDF alt must reach a record whose src is a relative path."""
        alt_block = {
            "image_records": [
                {"src": "../images/figure_1.png",
                 "file_path": "/out/images/figure_1.png",
                 "alt_text": "", "outcome": "skipped", "skip_reason": "x"},
            ]
        }
        struct = {"elements": [
            {"type": "PictureItem", "image_ref": "/out/images/figure_1.png",
             "alt_source": "source_pdf", "alt_text": "Author's own words."},
        ]}
        ensure_image_editor_records(
            alt_block, ["/out/images/figure_1.png"], struct_data=struct
        )
        records = alt_block["image_records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["alt_text"], "Author's own words.")
        self.assertEqual(records[0]["outcome"], "harvested")

    def test_data_uri_src_neither_matches_nor_crashes(self):
        """A data-URI record can't claim a basename, so the file is appended."""
        alt_block = {"image_records": [
            _generated_record("data:image/png;base64,iVBORw0KGgoAAAANS")
        ]}
        ensure_image_editor_records(
            alt_block, ["/out/images/figure_1.png"], struct_data=None
        )
        records = alt_block["image_records"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["src"], "figure_1.png")

    def test_idempotent_on_rerun(self):
        """Calling twice (e.g. Update PDF re-tag) must not grow the list."""
        alt_block = {"image_records": [_generated_record("../images/figure_1.png")]}
        files = ["/out/images/figure_1.png", "/out/images/figure_2.png"]
        ensure_image_editor_records(alt_block, files, struct_data=None)
        ensure_image_editor_records(alt_block, files, struct_data=None)
        self.assertEqual(len(alt_block["image_records"]), 2)


if __name__ == "__main__":
    unittest.main()
