"""
One-time interactive Garmin authentication.

Run this inside the container when you first set up, or if the saved session
expires and MFA is required:

    docker compose exec health_service python setup_auth.py

This will prompt for MFA if Garmin requires it. The session token is saved to
/data/garth_session so subsequent automated syncs skip re-authentication.
"""

import json
import subprocess
from pathlib import Path

ENCRYPTED_CREDS = Path("/run/secrets/garmin_credentials")
AGE_KEY = Path("/run/secrets/age_key")
SESSION_FILE = Path("/data/garth_session")


def main():
    if not ENCRYPTED_CREDS.exists():
        print(f"ERROR: encrypted credentials not found at {ENCRYPTED_CREDS}")
        print("On the host, run: age -R secrets/age.key.pub secrets/garmin.json > secrets/garmin.json.age")
        return
    if not AGE_KEY.exists():
        print(f"ERROR: age key not found at {AGE_KEY}")
        return

    result = subprocess.run(
        ["age", "--decrypt", "-i", str(AGE_KEY), str(ENCRYPTED_CREDS)],
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"ERROR: decryption failed: {result.stderr.decode().strip()}")
        return

    creds = json.loads(result.stdout)
    email = creds.get("email", "")
    password = creds.get("password", "")

    if not email or email == "your-garmin-email@example.com":
        print("ERROR: update secrets/garmin.json with your real Garmin credentials first.")
        return

    from garminconnect import Garmin

    print(f"Authenticating as {email}...")
    client = Garmin(email, password)
    client.login()

    SESSION_FILE.write_text(client.garth.dumps())
    print(f"Session saved to {SESSION_FILE}")
    print(f"Logged in as: {client.display_name}")
    print("Automated syncs will now use this session without prompting for MFA.")


if __name__ == "__main__":
    main()
