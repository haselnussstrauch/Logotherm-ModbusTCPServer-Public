"""Launcher: Update-Prüfung – die Logik steht in ``gateway/updater.py`` (gemeinsam mit dem Dienst)."""

from gateway.updater import *  # noqa: F401,F403
from gateway.updater import (  # noqa: F401  (ausdrücklich, für Tests/Typprüfung)
    APP_ID, CHECK_INTERVAL_S, UPDATE_REPO, ReleaseInfo, UpdateError, download, expected_sha256,
    fetch_latest, installed_version, is_newer, parse_version, run_installer,
)
