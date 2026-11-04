# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for logging_util.outcome_recorder.phase_guard."""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logging_util.outcome_recorder import phase_guard  # noqa: E402

_LOG = logging.getLogger("test.phase_guard")


def test_success_records_status_and_time():
    ps = {}
    with phase_guard(ps, "docling", logger=_LOG):
        pass
    assert ps["docling"]["status"] == "success"
    assert "time_s" in ps["docling"]


def test_failure_is_swallowed_and_recorded():
    ps = {}
    with phase_guard(ps, "remediation", logger=_LOG):
        raise ValueError("kaboom")
    # no exception propagated
    assert ps["remediation"]["status"] == "failed"
    assert "kaboom" in ps["remediation"]["error"]
    assert ps["remediation"]["type"] == "ValueError"
    assert ps["remediation"]["reason"] == "remediation_failed"


def test_reraise_propagates():
    ps = {}
    with pytest.raises(ValueError):
        with phase_guard(ps, "wcag_pre_audit", logger=_LOG, reraise=True):
            raise ValueError("x")
    assert ps["wcag_pre_audit"]["status"] == "failed"


def test_memory_error_maps_to_oom_reason():
    ps = {}
    with phase_guard(ps, "tagging", logger=_LOG):
        raise MemoryError("heap")
    assert ps["tagging"]["reason"] == "oom_or_memory_error"


def test_keyboard_interrupt_not_swallowed():
    ps = {}
    with pytest.raises(KeyboardInterrupt):
        with phase_guard(ps, "docling", logger=_LOG):
            raise KeyboardInterrupt()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
