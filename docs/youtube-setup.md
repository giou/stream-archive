# YouTube restream and VOD upload

Do this procedure when `output_mode` is `youtube` or `both`, or when you want VOD uploads of finished recordings. The setup wizard runs this flow as one of its steps. It is the recommended path. To run it alone, do the steps that follow.

1. Create a Google Cloud project. Enable the **YouTube Data API v3**.
2. Download an OAuth desktop client as `client_secret.json`. Google gives the steps in [the OAuth client guide](https://developers.google.com/youtube/registering_an_application). Place the file in the data directory before step 4. The default name is `client_secret.json` (`youtube.client_secrets_file` names it).
3. Publish the OAuth consent screen: **Google Cloud Console → APIs & Services → OAuth consent screen → Audience tab → Publishing status → Publish app**.

   While the app is in *Testing*, refresh tokens expire after 7 days and only test users can authorize. Publishing keeps the token valid.
4. Run the one-time authorization flow:

   ```sh
   docker compose run --rm stream-archive stream-archive-setup-youtube
   ```

   The command prints the authorization URL and tries to open it in your browser. No browser runs inside `compose run`, so open the printed URL on your own machine. After you authorize, the redirect page shows "Authorization successful!" and the command saves the token to `youtube_token.json`.
5. If the redirect page does not load (SSH session, headless host, Docker), copy the full URL from the address bar. Paste the URL at the prompt.

Under Docker, the localhost redirect cannot reach the container. Always paste the full URL in that case. The paste fallback is the path, not the exception.

The token refreshes automatically while it is refreshable. If the token expires beyond refresh, run the command again.

The same login enables VOD uploads. The **Upload** button on a recording (Telegram **Recordings** menu and web panel) sends the finished file to YouTube as a video. The title comes from the file name. The privacy follows `youtube.privacy_status`. The per-channel auto-upload (`/vodupload`, panel channel card) uploads each finished recording at once. Each upload costs YouTube API quota: one `videos.insert` call per file.
