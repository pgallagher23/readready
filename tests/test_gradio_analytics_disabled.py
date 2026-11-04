# SPDX-License-Identifier: AGPL-3.0-or-later
"""Neither web UI may phone home.

Gradio's usage analytics and version check send requests to api.gradio.app and
huggingface.co on every start and launch. A self-hosted accessibility service
must not report its usage to a third party, so both apps turn them off.
"""


def test_main_app_has_analytics_disabled():
    import app
    assert app.demo.analytics_enabled is False

