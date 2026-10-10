# YouTube integration — one-time Google setup

Do this once for the stake. Both appliances share the same OAuth client.

1. Go to console.cloud.google.com and create a project (e.g. "Snow Springs Stake Streaming").
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. APIs & Services → OAuth consent screen: User type **External**. App name
   **Snow Springs Stake Streaming**, plus a support email. App domain links (all one page, `main` branch):
   homepage `https://github.com/heneryville/ndi-rmtp/blob/main/docs/youtube-app.md`,
   privacy `…/youtube-app.md#privacy`, terms `…/youtube-app.md#terms`. Scopes: add `https://www.googleapis.com/auth/youtube`.
4. On the consent screen, click **Publish app** so the status is **In production**.
   Do NOT submit for verification. Leaving it in Testing makes every ward's sign-in
   expire after 7 days.
5. APIs & Services → Credentials → Create credentials → OAuth client ID → type
   **TVs and Limited Input devices**. Copy the client ID and secret.
6. In your local `config.south.toml` and `config.north.toml` (never the
   `.example` files), add:

       [youtube]
       client_id = "…apps.googleusercontent.com"
       client_secret = "…"

   Bump `config_version`, note it in `CONFIG_CHANGELOG.md`, and deploy per `deploy.md`.
7. On each appliance, open **Ward setup → Connect YouTube** for each ward. Sign in as
   the ward's YouTube account. Google will say the app isn't verified: choose
   **Advanced → Go to (app name)**. Pick the ward's channel if asked.

Quota: the default 10,000 units/day covers about 8 wards streaming weekly. Check
APIs & Services → YouTube Data API v3 → Quotas after the first Sunday.

To undo: Disconnect on Ward setup, or remove `[youtube]` from the config to hide the
feature entirely (wards fall back to pasted keys).
