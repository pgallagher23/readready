# SPDX-License-Identifier: AGPL-3.0-or-later
import app


def test_defaults_to_loopback_without_auth():
    assert app._launch_settings({}) == {"server_name": "127.0.0.1", "auth": None}


def test_host_and_login_from_env():
    s = app._launch_settings({"READREADY_HOST": "0.0.0.0",
                              "READREADY_USER": "ops", "READREADY_SECRET": "s3cret"})
    assert s == {"server_name": "0.0.0.0", "auth": ("ops", "s3cret")}


def test_secret_without_user_uses_default_user():
    assert app._launch_settings({"READREADY_SECRET": "x"})["auth"] == ("readready", "x")


def test_warns_when_exposed_without_login(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        app._launch_settings({"READREADY_HOST": "0.0.0.0"})
    assert any("READREADY_SECRET" in r.getMessage() for r in caplog.records)


def test_no_warning_on_loopback_or_with_login(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        app._launch_settings({})
        app._launch_settings({"READREADY_HOST": "0.0.0.0", "READREADY_SECRET": "s"})
    assert not [r for r in caplog.records if "READREADY_SECRET" in r.getMessage()]
