#!/usr/bin/env python3
"""Interactive helper to obtain a Google Drive OAuth refresh token."""
import argparse
import getpass
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    parser = argparse.ArgumentParser(
        description="Get a Google Drive refresh token for the Pinteresto uploader."
    )
    parser.add_argument(
        "--redirect-uri",
        default="http://localhost:8765/",
        help="OAuth redirect URI registered for your desktop client.",
    )
    args = parser.parse_args()

    client_id = input("Google OAuth client ID: ").strip()
    client_secret = getpass.getpass("Google OAuth client secret: ").strip()
    if not client_id or not client_secret:
        parser.error("Client ID and client secret are required.")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [args.redirect_uri],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = args.redirect_uri
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    print("\nOpen this URL, approve Drive access, then copy the full redirected URL:")
    print(authorization_url)
    response = input("\nPaste the redirected URL or authorization code: ").strip()
    if not response:
        print("No authorization response was provided.", file=sys.stderr)
        return 1

    try:
        if response.startswith(("http://", "https://")):
            flow.fetch_token(authorization_response=response)
        else:
            flow.fetch_token(code=response)
    except Exception as error:
        print(f"Token exchange failed: {error}", file=sys.stderr)
        return 1

    refresh_token = flow.credentials.refresh_token
    if not refresh_token:
        print(
            "Google did not return a refresh token. Revoke prior consent and "
            "rerun with consent enabled.",
            file=sys.stderr,
        )
        return 1
    print("\nGOOGLE_OAUTH_REFRESH_TOKEN (store this in your secrets manager):")
    print(refresh_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
