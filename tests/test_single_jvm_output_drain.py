# SPDX-License-Identifier: AGPL-3.0-or-later
"""The single-JVM tagger must not stall when Java prints a lot before analysis.

Python waits for the analysis file while Java runs; if nobody reads Java's
stdout/stderr, a full pipe (64 KB) blocks Java and the analysis file never
appears until the wait times out.
"""
import subprocess
import sys
import time

from output_gen import pdf_tagger

FAKE_JVM = r'''
import os, sys, time
analysis, enriched, output = sys.argv[1:4]
sys.stdout.write("progress line\n" * 25000)   # ~350 KB, far more than a pipe holds
sys.stdout.flush()
sys.stderr.write("warning line\n" * 25000)
sys.stderr.flush()
open(analysis, "w").write('{"summary": {}, "pages": 0}')
while not os.path.exists(enriched):
    time.sleep(0.05)
open(output, "wb").write(b"%PDF-1.4\n%%EOF\n")
'''


def test_tagger_output_is_drained_while_waiting(tmp_path, monkeypatch):
    script = tmp_path / "fake_jvm.py"
    script.write_text(FAKE_JVM)
    output_pdf = str(tmp_path / "out.pdf")
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kw):
        return real_popen([sys.executable, str(script), output_pdf + ".analysis.json",
                           output_pdf + ".enriched.json", output_pdf], **kw)

    monkeypatch.setattr(pdf_tagger.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(pdf_tagger, "_do_matching_and_gap_recovery",
                        lambda *a, **k: ({}, {}, []))
    monkeypatch.setattr(pdf_tagger, "_validate_struct_tree", lambda *a, **k: [])
    monkeypatch.setattr(pdf_tagger, "_maybe_reconcile_widths", lambda *a, **k: None)

    start = time.monotonic()
    result = pdf_tagger._run_single_jvm_content_preserving(
        "unused.jar", "in.pdf", "structure.json", output_pdf, "en", None,
        "1g", 8, "in.pdf", 80.0)
    assert result is not None, "tagger stalled on a full output pipe"
    assert result["path"] == output_pdf
    assert time.monotonic() - start < 8
