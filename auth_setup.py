"""One-shot Garmin auth setup using garth directly.
Run this once to cache tokens, then the poller works headlessly forever.
"""
import os
import pathlib
from dotenv import load_dotenv
import garth

load_dotenv()

email = os.environ["GARMIN_EMAIL"]
password = os.environ["GARMIN_PASSWORD"]
token_dir = pathlib.Path(os.environ.get("GARMIN_TOKEN_DIR", "~/.garminconnect")).expanduser()
token_dir.mkdir(parents=True, exist_ok=True)

print(f"Logging in as {email}...")
garth.login(email, password)
garth.save(str(token_dir))
print(f"✅ Tokens saved to {token_dir}")
print("The poller will now work without MFA.")
