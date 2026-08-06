import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube"]


def main():
    config_path = Path("config.json")
    if not config_path.exists():
        print("ERROR: config.json not found", file=sys.stderr)
        sys.exit(1)
    with open(config_path) as f:
        config = json.load(f)

    secrets_file = config.get("youtube", {}).get("client_secrets_file", "client_secret.json")
    secrets_path = Path(secrets_file)
    if not secrets_path.exists():
        print(f"ERROR: {secrets_file} not found", file=sys.stderr)
        sys.exit(1)

    print("Starting YouTube OAuth setup...")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    flow.redirect_uri = "http://localhost"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print("1. Visit this URL in a browser:")
    print(f"   {auth_url}")
    print()
    print("2. After authorizing, you will be redirected to a page that shows an authorization code.")
    print("   (or the code may appear in the redirect URL after 'code=')")
    print("3. Paste the authorization code below:")
    print()
    code = input("   Code: ").strip()

    flow.fetch_token(code=code)

    token_path = Path("youtube_token.json")
    data = json.loads(flow.credentials.to_json())
    with open(token_path, "w") as f:
        json.dump(data, f)
    token_path.chmod(0o600)

    print()
    print(f"Token saved to {token_path}")
    print("YouTube authentication complete.")


if __name__ == "__main__":
    main()
