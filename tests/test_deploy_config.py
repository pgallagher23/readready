# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deployment files must not ship known-default secrets or run as root."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_compose_requires_redis_password():
    text = (ROOT / "docker-compose.yml").read_text()
    assert ":-changeme" not in text
    assert "${REDIS_PASSWORD:?" in text


def test_env_example_has_no_default_password():
    assert "changeme" not in (ROOT / ".env.example").read_text()


def test_compose_passes_anthropic_key():
    assert "ANTHROPIC_API_KEY" in (ROOT / "docker-compose.yml").read_text()


def _dockerfile_lines():
    return [ln.strip() for ln in (ROOT / "Dockerfile").read_text().splitlines()]


def _run_instructions():
    """Dockerfile RUN instructions with continuations joined, comments dropped."""
    runs, cur = [], None
    for ln in _dockerfile_lines():
        if ln.startswith("#") or (cur is None and not ln.startswith("RUN ")):
            continue
        cur = (cur + " " if cur is not None else "") + ln.rstrip("\\").strip()
        if not ln.endswith("\\"):
            runs.append(cur)
            cur = None
    return runs


def _compose():
    import yaml
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_image_runs_as_non_root():
    users = [ln.split()[1] for ln in _dockerfile_lines() if ln.startswith("USER ")]
    assert users and users[-1] == "readready"


def test_prefetched_image_runs_offline():
    text = (ROOT / "Dockerfile").read_text()
    assert "HF_HUB_OFFLINE=${PREFETCH_MODELS}" in text
    assert "TRANSFORMERS_OFFLINE=${PREFETCH_MODELS}" in text


def test_model_cache_is_a_writable_volume_for_app_and_worker():
    compose = _compose()
    assert "hf-cache" in compose["volumes"]
    for svc in ("app", "worker"):
        assert "hf-cache:/app/.hf" in compose["services"][svc]["volumes"], svc


def test_model_cache_chown_is_in_the_prefetch_layer():
    # A separate `chown -R` layer would duplicate the model files in the image.
    text = (ROOT / "Dockerfile").read_text()
    runs = _run_instructions()
    prefetch = [r for r in runs if "prefetch_models.py" in r]
    assert len(prefetch) == 1 and "chown -R readready /app/.hf" in prefetch[0]
    assert not any("/app/.hf" in r and "chown" in r for r in runs if r not in prefetch)
    # The user must exist before the prefetch layer chowns to it.
    assert text.index("useradd") < text.index("prefetch_models.py")


def test_font_dirs_writable_without_tmpfs():
    text = (ROOT / "Dockerfile").read_text()
    assert "chown readready /usr/local/share/fonts /var/cache/fontconfig" in text


def test_entrypoint_font_link_is_not_fatal():
    text = (ROOT / "docker" / "entrypoint.sh").read_text()
    # Under `set -e` a failing link (non-root, no tmpfs) would stop the
    # container; it must be the condition of an `if` that warns instead.
    flat = " ".join(text.split())
    assert "ln -sfn /app/fonts /usr/local/share/fonts/readready-user" in flat
    link_at = flat.index("ln -sfn /app/fonts")
    assert flat.rfind("if ", 0, link_at) > flat.rfind("fi", 0, link_at)
    assert "warning" in flat[link_at:flat.index("fi", link_at)]


def test_prefetch_covers_every_layout_model():
    import re
    prefetch = (ROOT / "scripts" / "prefetch_models.py").read_text()
    conv = (ROOT / "converter" / "docling_converter.py").read_text()
    used = set(re.findall(r'"\w+":\s*(DOCLING_LAYOUT_\w+)', conv))
    assert used
    for spec in used:
        assert spec in prefetch, spec


def test_caddy_sets_security_headers_and_no_plugin_directives():
    text = (ROOT / "Caddyfile").read_text()
    assert "X-Content-Type-Options" in text and "request_body" in text
    assert "rate_limit" not in text


def test_image_binds_all_interfaces_by_default():
    """app.py defaults to loopback, which is unreachable inside a container.

    A plain ``docker run -p 7860:7860`` must work without compose, so the
    image itself sets READREADY_HOST; compose/.env may still override it.
    """
    env = {}
    for ln in _dockerfile_lines():
        if ln.startswith("ENV "):
            for kv in ln[4:].split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    env[k] = v
    assert env.get("READREADY_HOST") == "0.0.0.0"
