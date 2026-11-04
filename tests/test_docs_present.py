# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_docs_present.py
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

def test_required_docs_exist_and_are_nonempty():
    for rel in ("LICENSE", "NOTICE", "README.md", "CONTRIBUTING.md", "CHANGELOG.md",
                ".github/workflows/ci.yml", ".github/dependabot.yml"):
        p = ROOT / rel
        assert p.is_file() and p.stat().st_size > 200, rel

def test_license_is_agpl_with_holder():
    t = (ROOT / "LICENSE").read_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in t
    # The appendix copyright line (after the FSF's own) names the same holder as NOTICE.
    holders = re.findall(r"Copyright \(C\) \d{4} (.+)", t)
    assert len(holders) == 2, holders
    assert holders[1].strip() in (ROOT / "NOTICE").read_text()

def test_ci_publish_job_is_gated():
    t = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "vars.PUBLISH_IMAGE == 'true'" in t

def test_ci_python_job_has_the_jar():
    import yaml
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert ci["jobs"]["python-tests"]["needs"] == "java"
    assert any("download-artifact" in str(s.get("uses", "")) for s in ci["jobs"]["python-tests"]["steps"])
