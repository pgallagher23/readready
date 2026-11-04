# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rotation transform round-trip tests for the overlay (scanned) path.

Content-stream coordinates (pre-rotation, MediaBox space) and Docling element
bboxes (post-rotation display space) are converted back and forth by two
functions that MUST be exact inverses:

* Python ``content_matcher._transform_run_coords`` — content -> display,
  used when matching runs to elements.
* Java ``FormImagePageTagger.displayToContent`` — display -> content, used
  when writing overlay MCR text back into the page.

If these ever drift apart, overlay text lands in the wrong place on rotated
pages (the exact failure class this suite guards).  The Java side is mirrored
here so the relationship is locked in a fast, dependency-free unit test.
"""
import pytest

from output_gen.content_matcher import _transform_run_coords


def _display_to_content(dx, dy, rotation, mb_w, mb_h):
    """Mirror of FormImagePageTagger.displayToContent (Java)."""
    r = rotation % 360
    if r == 90:
        return (mb_w - dy, dx)
    if r == 180:
        return (mb_w - dx, mb_h - dy)
    if r == 270:
        return (dy, mb_h - dx)
    return (dx, dy)


# Landscape MediaBox (matches rotated scanned-book pages: 792x612).
MB_W, MB_H = 792.0, 612.0
SAMPLE_POINTS = [(37.0, 96.67), (400.0, 300.0), (0.0, 0.0), (792.0, 612.0),
                 (150.5, 575.25)]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("cx,cy", SAMPLE_POINTS)
def test_transform_round_trip_is_identity(rotation, cx, cy):
    """content -> display -> content must return the original point."""
    page_rotations = {1: (rotation, MB_W, MB_H)}
    dx, dy = _transform_run_coords(cx, cy, 1, page_rotations)
    rx, ry = _display_to_content(dx, dy, rotation, MB_W, MB_H)
    assert rx == pytest.approx(cx, abs=1e-3)
    assert ry == pytest.approx(cy, abs=1e-3)


def test_rotation_270_maps_top_of_page_to_expected_content_coords():
    """Explicit 270 anchor check (the common scanned-book case).

    Display top-left-ish (37, 96.67) on a 270 page must map into on-page
    MediaBox coordinates (both within the landscape 792x612 box), not off-page
    negatives — off-page overlay text is not extractable.
    """
    dx, dy = _transform_run_coords(37.0, 96.67, 1, {1: (270, MB_W, MB_H)})
    cx, cy = _display_to_content(dx, dy, 270, MB_W, MB_H)
    assert 0 <= cx <= MB_W
    assert 0 <= cy <= MB_H
    # And it is a true inverse.
    assert (cx, cy) == pytest.approx((37.0, 96.67), abs=1e-3)
