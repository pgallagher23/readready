# SPDX-License-Identifier: AGPL-3.0-or-later
"""OOM retry policy: heap exhaustion and container OOM-kill are different bugs.

A JVM that hits its own -Xmx ceiling (``OutOfMemoryError``) and a JVM the
kernel SIGKILLs for exceeding its cgroup limit look similar in the logs but
need opposite responses:

* container OOM-kill -> the JVM's footprint is too big for the cgroup, so
  *shrink* the heap to fit.
* heap exhaustion -> the ceiling was already too low, so shrinking it
  guarantees the retry fails too, and costs a full second JVM run
  (minutes on a large PDF).

Regression cover for the second case, which used to be halved as well.
"""

import re
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import output_gen.pdf_tagger as pdf_tagger


CONTAINER_KILL_RC = -9  # SIGKILL from the cgroup OOM killer
JVM_OOM_RC = 3          # -XX:+ExitOnOutOfMemoryError exit code
JVM_OOM_STDERR = "Terminating due to java.lang.OutOfMemoryError: Java heap space"
# The analyzer omits ExitOnOutOfMemoryError, so it dies with a raw trace and rc=1
ANALYZER_OOM_RC = 1
ANALYZER_OOM_STDERR = 'Exception in thread "main" java.lang.OutOfMemoryError: Java heap space'


def _mb(heap):
    """Parse a heap string like '4g' / '4704m' into MB, for test assertions."""
    m = re.match(r"(\d+)\s*(g|m)", heap.strip().lower())
    assert m, f"unparseable heap string: {heap!r}"
    val, unit = int(m.group(1)), m.group(2)
    return val * 1024 if unit == "g" else val


@pytest.fixture
def g1_default(monkeypatch):
    """A machine big enough that JVM ergonomics picks G1, not SerialGC."""
    monkeypatch.setattr(pdf_tagger, "_serial_gc_is_default", lambda: False)


class TestClassifyOom:
    def test_sigkill_is_a_container_oom(self):
        assert pdf_tagger._classify_oom(CONTAINER_KILL_RC, "") == "container"

    def test_jvm_outofmemoryerror_is_a_heap_oom(self):
        assert pdf_tagger._classify_oom(JVM_OOM_RC, JVM_OOM_STDERR) == "heap"

    def test_analyzer_raw_oom_trace_is_a_heap_oom(self):
        """The analyzer has no ExitOnOutOfMemoryError, so it exits 1 with a trace."""
        assert pdf_tagger._classify_oom(ANALYZER_OOM_RC, ANALYZER_OOM_STDERR) == "heap"

    def test_ordinary_failure_is_not_an_oom(self):
        assert pdf_tagger._classify_oom(1, "NullPointerException at Foo.java:12") is None

    def test_clean_exit_is_not_an_oom(self):
        assert pdf_tagger._classify_oom(0, "") is None

    def test_timeout_returncode_none_is_not_an_oom(self):
        assert pdf_tagger._classify_oom(None, "") is None


class TestOomRetryHeap:
    def test_heap_exhaustion_never_shrinks_the_heap(self, g1_default):
        """The bug: halving a ceiling that was already too low."""
        retry = pdf_tagger._oom_retry_heap("heap", "4704m")
        assert retry is not None, "a heap OOM should still get its one GC retry"
        assert _mb(retry) >= _mb("4704m"), (
            f"retry heap {retry} is smaller than the 4704m that already "
            f"proved insufficient - the retry cannot succeed"
        )

    def test_container_kill_shrinks_the_heap_to_fit_the_cgroup(self):
        assert pdf_tagger._oom_retry_heap("container", "4g") == "2g"

    def test_non_oom_failure_gets_no_retry(self):
        assert pdf_tagger._oom_retry_heap(None, "4g") is None

    def test_heap_retry_skipped_when_serialgc_is_already_the_default(self, monkeypatch):
        """On a small container the retry would be an identical command line."""
        monkeypatch.setattr(pdf_tagger, "_serial_gc_is_default", lambda: True)
        assert pdf_tagger._oom_retry_heap("heap", "512m") is None

    def test_container_retry_survives_a_serialgc_default(self, monkeypatch):
        """Shrinking to fit the cgroup is still worth doing regardless of GC."""
        monkeypatch.setattr(pdf_tagger, "_serial_gc_is_default", lambda: True)
        assert pdf_tagger._oom_retry_heap("container", "4g") == "2g"


class TestSerialGcIsDefault:
    def test_small_memory_machine_defaults_to_serialgc(self, monkeypatch):
        monkeypatch.setattr(pdf_tagger, "get_system_ram_mb", lambda: 1024)
        monkeypatch.setattr(os, "cpu_count", lambda: 8)
        assert pdf_tagger._serial_gc_is_default() is True

    def test_single_cpu_machine_defaults_to_serialgc(self, monkeypatch):
        monkeypatch.setattr(pdf_tagger, "get_system_ram_mb", lambda: 10240)
        monkeypatch.setattr(os, "cpu_count", lambda: 1)
        assert pdf_tagger._serial_gc_is_default() is True

    def test_production_worker_defaults_to_g1(self, monkeypatch):
        """The 10g / 10-CPU prod worker gets G1, so the SerialGC retry is real."""
        monkeypatch.setattr(pdf_tagger, "get_system_ram_mb", lambda: 10240)
        monkeypatch.setattr(os, "cpu_count", lambda: 10)
        assert pdf_tagger._serial_gc_is_default() is False


class TestDescribeTaggerFailure:
    """A swallowed OOM is what let the production bug hide for so long."""

    def test_heap_exhaustion_names_the_cause_and_the_remedy(self):
        msg = pdf_tagger._describe_tagger_failure(ANALYZER_OOM_RC, ANALYZER_OOM_STDERR)
        assert "heap" in msg.lower()
        assert "python_worker_mb" in msg or "memory limit" in msg

    def test_container_kill_is_reported_as_the_oom_killer(self):
        msg = pdf_tagger._describe_tagger_failure(CONTAINER_KILL_RC, "")
        assert "OOM killer" in msg or "oom killer" in msg.lower()

    def test_ordinary_failure_just_reports_the_return_code(self):
        msg = pdf_tagger._describe_tagger_failure(1, "NullPointerException")
        assert "rc=1" in msg
        assert "heap" not in msg.lower()
