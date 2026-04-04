"""
Infisical client — Universal Auth + secret fetch via REST API.

Each service that needs secrets gets a copy of this file and its own
machine identity (client ID + client secret) scoped to only its secrets.

Auth flow:
  1. POST /api/v1/auth/universal-auth/login  →  short-lived access token (15 min)
  2. GET  /api/v3/secrets/raw                →  { KEY: value, ... }

Configuration (environment variables):
  INFISICAL_URL          base URL of your Infisical instance
  INFISICAL_PROJECT_ID   project UUID (shown in Project Settings)
  INFISICAL_ENVIRONMENT  environment slug (default: prod)
  INFISICAL_SECRET_PATH  secret path (default: /)

Docker secrets (files mounted at /run/secrets/):
  infisical_client_id
  infisical_client_secret
"""

import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_CLIENT_ID_FILE     = Path("/run/secrets/infisical_client_id")
_CLIENT_SECRET_FILE = Path("/run/secrets/infisical_client_secret")

_INFISICAL_URL  = os.getenv("INFISICAL_URL",  "http://localhost:8200")
_PROJECT_ID     = os.getenv("INFISICAL_PROJECT_ID", "")
_ENVIRONMENT    = os.getenv("INFISICAL_ENVIRONMENT", "prod")
_SECRET_PATH    = os.getenv("INFISICAL_SECRET_PATH", "/")


def get_secrets() -> dict[str, str]:
    """
    Authenticate and return all secrets at the configured path as a plain dict.
    Raises clearly if credentials or config are missing.
    """
    if not _PROJECT_ID or _PROJECT_ID == "CHANGE_ME_AFTER_SETUP":
        raise RuntimeError(
            "INFISICAL_PROJECT_ID is not set. "
            "Complete Infisical setup, then add the project UUID to docker-compose.yml."
        )

    for f in (_CLIENT_ID_FILE, _CLIENT_SECRET_FILE):
        if not f.exists():
            raise FileNotFoundError(
                f"{f} not found. "
                "Create secrets/infisical_client_id and secrets/infisical_client_secret "
                "with the machine identity credentials from Infisical."
            )

    client_id     = _CLIENT_ID_FILE.read_text().strip()
    client_secret = _CLIENT_SECRET_FILE.read_text().strip()

    with httpx.Client(timeout=10) as client:
        # Step 1 — exchange machine identity credentials for a short-lived access token
        auth_resp = client.post(
            f"{_INFISICAL_URL}/api/v1/auth/universal-auth/login",
            json={"clientId": client_id, "clientSecret": client_secret},
        )
        auth_resp.raise_for_status()
        access_token = auth_resp.json()["accessToken"]

        # Step 2 — fetch secrets at the configured path
        secrets_resp = client.get(
            f"{_INFISICAL_URL}/api/v3/secrets/raw",
            params={
                "workspaceId": _PROJECT_ID,
                "environment": _ENVIRONMENT,
                "secretPath": _SECRET_PATH,
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        secrets_resp.raise_for_status()

    secrets = {
        s["secretKey"]: s["secretValue"]
        for s in secrets_resp.json().get("secrets", [])
    }
    logger.info(f"Fetched {len(secrets)} secret(s) from Infisical ({_SECRET_PATH})")
    return secrets
