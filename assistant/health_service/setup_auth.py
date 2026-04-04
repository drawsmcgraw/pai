"""
One-time interactive Garmin authentication.

Run this inside the container when you first set up, or if the saved tokens
expire and MFA is required:

    docker compose -f docker-compose.setup-auth.yml run --rm garmin_setup

Tokens are saved to /data/garmin_tokens.json. Subsequent automated syncs
load them automatically without re-authenticating.

If Garmin's SSO returns a 429 (Cloudflare rate limit), the script will
wait and retry with exponential backoff. Installing curl_cffi (included
in requirements) enables TLS fingerprint impersonation which greatly
reduces the chance of being blocked.
"""

import time
from pathlib import Path

TOKEN_FILE = Path("/data/garmin_tokens.json")

_MAX_RETRIES = 5
_INITIAL_WAIT = 30  # seconds


def _login_with_retry(client):
    """Attempt login with exponential backoff on 429 / rate-limit errors."""
    wait = _INITIAL_WAIT
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            client.login(tokenstore=str(TOKEN_FILE))
            return
        except Exception as e:
            msg = str(e).lower()
            is_rate_limit = "429" in msg or "rate" in msg or "cloudflare" in msg or "too many" in msg
            if is_rate_limit and attempt < _MAX_RETRIES:
                print(f"Rate limited (attempt {attempt}/{_MAX_RETRIES}). "
                      f"Waiting {wait}s before retry...")
                time.sleep(wait)
                wait *= 2
            else:
                raise


def main():
    from infisical import get_secrets
    try:
        secrets = get_secrets()
    except Exception as e:
        print(f"ERROR fetching credentials from Infisical: {e}")
        return

    email    = secrets.get("GARMIN_EMAIL", "")
    password = secrets.get("GARMIN_PASSWORD", "")

    if not email:
        print("ERROR: GARMIN_EMAIL not found in Infisical.")
        return

    from garminconnect import Garmin

    print(f"Authenticating as {email}...")
    print("(curl_cffi TLS impersonation active — Cloudflare bypass enabled)")
    client = Garmin(email, password)
    _login_with_retry(client)

    print(f"Tokens saved to {TOKEN_FILE}")
    print(f"Logged in as: {client.display_name}")
    print("Automated syncs will now use these tokens without re-authenticating.")


if __name__ == "__main__":
    main()
