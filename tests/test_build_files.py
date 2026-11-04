# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

def test_dockerfile_is_multistage_and_has_no_font_symlink():
    d = (ROOT / "Dockerfile").read_text()
    assert d.count("\nFROM ") + d.startswith("FROM ") >= 2
    assert "maven" in d.lower() and "COPY --from=" in d
    assert "windows-compat" not in d
    assert 'ENTRYPOINT ["/app/docker/entrypoint.sh"]' in d

def test_compose_mounts_fonts_readonly():
    c = (ROOT / "docker-compose.yml").read_text()
    assert "./fonts:/app/fonts:ro" in c

def test_makefile_targets():
    m = (ROOT / "Makefile").read_text()
    for t in ("jar:", "test:", "fonts:", "fonts-check:", "docker:", "sample:"):
        assert t in m

def test_jar_is_ignored_in_public_gitignore():
    assert "output_gen/itext_tagger.jar" in (ROOT / ".gitignore").read_text()

def test_dockerignore_keeps_fonts():
    assert "fonts/" not in (ROOT / ".dockerignore").read_text()

def test_env_example_lists_every_variable_the_code_reads():
    t = (ROOT / ".env.example").read_text()
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "REDIS_PASSWORD",
                "REDIS_URL",
                "READREADY_CONFIG", "READREADY_FONT_DIR"):
        assert f"{var}=" in t, var

def test_entrypoint_restores_texgyre_and_dockerignore_skips_maven_target():
    e = (ROOT / "docker" / "entrypoint.sh").read_text()
    assert "texgyre" in e and "set -e" in e and 'exec "$@"' in e
    assert "output_gen/itext_src/target/" in (ROOT / ".dockerignore").read_text().splitlines()
    m = (ROOT / "Makefile").read_text()
    assert "DOCKER ?= docker" in m and "$(DOCKER) build" in m
