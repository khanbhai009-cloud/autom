# Trend Drive setup

1. Create a Drive parent folder, share it with the service-account email in `GOOGLE_CREDS_JSON` as Editor, and set its ID as `TREND_DRIVE_PARENT_ID`.
2. Create a Google OAuth Desktop client with the Drive scope; run `python scripts/get_drive_refresh_token.py` on a machine where you can open the authorization link.
3. Add `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and the generated `GOOGLE_OAUTH_REFRESH_TOKEN` to Replit Secrets; keep `GOOGLE_CREDS_JSON` and `SPREADSHEET_ID` configured for Sheets and background image analysis.
4. If using a forwarded Codespaces callback, register its exact URL as an OAuth redirect URI and pass it with `--redirect-uri`; copy the full callback URL from the browser into the helper.
5. Open `/trends-ui`, add a trend, and upload its images; for Google OAuth apps in Testing mode, refresh tokens may expire after seven days, so publish the consent screen for ongoing use.
