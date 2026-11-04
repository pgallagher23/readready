# SPDX-License-Identifier: AGPL-3.0-or-later
"""veraPDF and PDF/UA-1 claims are not part of the public product."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_verapdf_client_is_gone():
    assert not (ROOT / "utils/verapdf_client.py").exists()


def test_compose_has_no_verapdf_service():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "verapdf" not in compose["services"]


def test_config_has_no_verapdf_keys():
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert not [k for k in config["output"] if "verapdf" in k]


def test_no_verapdf_in_entry_points_or_gates():
    for rel in ("main.py", "app.py", "pipeline_core.py",
                "validation/gates.py", "logging_util/failure_taxonomy.py"):
        text = (ROOT / rel).read_text().lower()
        code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
        assert not [l for l in code if "verapdf" in l], rel


def test_user_facing_text_makes_no_pdfua_claim():
    pattern = re.compile(r"pdf/ua|verapdf", re.I)
    for rel in ("README.md", "CHANGELOG.md", "NOTICE", "LICENSE", "pyproject.toml",
                "fonts/README.md", "scripts/import_fonts.py", "static/landing_page.html"):
        assert not pattern.search((ROOT / rel).read_text()), rel


def test_html_pdf_metadata_has_no_pdfua_identifier():
    import fitz
    from output_gen.html_to_pdf import _inject_xmp_metadata
    doc = fitz.open()
    doc.new_page()
    out = fitz.open(stream=_inject_xmp_metadata(doc.tobytes()), filetype="pdf")
    xmp = out.get_xml_metadata()
    assert "dc:language" in xmp
    assert "pdfuaid" not in xmp


def test_taggers_open_output_without_inherited_conformance():
    src = ROOT / "output_gen/itext_src/src/main/java/pdfaccessibility"
    for rel in ("PdfAccessibilityTagger.java", "ContentPreservingTagger.java"):
        text = re.sub(r"\s+", " ", (src / rel).read_text())
        assert "new PdfDocument(reader, writer" not in text, rel
        assert "new PdfDocument( new PdfReader(inputPath), new PdfWriter" not in text, rel
