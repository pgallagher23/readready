# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 5 must not report success when every image failed (G-31).

Run: python -m pytest tests/test_phase_status_vlm.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline_core import _vlm_phase_status


def test_all_images_failed_is_not_success():
    status = _vlm_phase_status({"updated_count": 0, "skipped_count": 12})
    assert status["status"] != "success", (
        "every image failing is not a successful phase"
    )
    assert status.get("reason")


def test_partial_failure_is_degraded_not_success():
    status = _vlm_phase_status({"updated_count": 9, "skipped_count": 3})
    assert status["status"] == "degraded"
    assert "3" in str(status.get("reason", "")) or status.get("skipped") == 3


def test_clean_run_is_success():
    status = _vlm_phase_status({"updated_count": 12, "skipped_count": 0})
    assert status["status"] == "success"


def test_no_images_is_success():
    status = _vlm_phase_status({"updated_count": 0, "skipped_count": 0})
    assert status["status"] == "success", "a document with no images is fine"


def test_explicit_error_still_failed():
    status = _vlm_phase_status(
        {"updated_count": 0, "skipped_count": 0, "error": "provider down"}
    )
    assert status["status"] == "failed"


# ── Images that already carry alt text are not failures ────────────────────
# The "all images already have proper alt text" early return used to report
# them as skipped_count=N, which the status above reads as "all N failed".
# On prod that false failure fired 21 times in a week (2026-10-05..12), every
# time on documents whose alt text came from the source PDF (Phase 3c).

def test_images_that_already_had_alt_are_success():
    status = _vlm_phase_status(
        {"updated_count": 0, "skipped_count": 0, "already_had_alt_count": 3}
    )
    assert status["status"] == "success"


class _ConnectedClient:
    base_url = "stub://vlm"

    def check_connection(self):
        return True


def test_generator_does_not_count_existing_alt_as_skipped(tmp_path, monkeypatch):
    import vlm.alt_text_generator as gen

    monkeypatch.setattr(gen, "create_vlm_client", lambda cfg: _ConnectedClient())
    html = tmp_path / "doc.html"
    html.write_text(
        '<html><body>'
        '<img src="images/figure_1.png" alt="Bar chart of enrollment by year">'
        '<img src="images/figure_2.png" alt="Campus map showing the library">'
        '</body></html>',
        encoding="utf-8",
    )

    result = gen.generate_alt_text_for_images(
        html_path=str(html), image_files=[], image_dir=str(tmp_path),
        llm_config={},
    )

    assert result["skipped_count"] == 0
    assert result["already_had_alt_count"] == 2
    assert _vlm_phase_status(result)["status"] == "success"
