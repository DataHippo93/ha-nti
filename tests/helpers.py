"""Shared helpers for the NTI net tests."""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_credentials() -> tuple[str, str]:
    """Return the NTI net account credentials from `.env`, if configured."""
    username = os.environ.get("NTI_USERNAME")
    password = os.environ.get("NTI_PASSWORD")

    if (not username or not password) and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() == "USERNAME":
                username = username or value
            elif key.strip() == "PASSWORD":
                password = password or value

    if not username or not password:
        import pytest

        pytest.skip("Set NTI_USERNAME/NTI_PASSWORD (or a .env file) to run live tests")
    return username, password
