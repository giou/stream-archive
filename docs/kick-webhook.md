# Kick webhook

The webhook gives near-instant live and offline signals, and Kick chat. The
poll alone cannot deliver chat, because Kick has no chat replay.

## How exposure works

The app binds two loopback listeners and publishes nothing itself:

- `endpoint.listen_host:endpoint.listen_port` (default `127.0.0.1:8787`):
  panel and control API.
- `kick.webhook.listen_host:listen_port` (default `127.0.0.1:8788`):
  `POST /kick/webhook` alone.

You publish them yourself. A common split: `tailscale serve` for the panel
port (tailnet only), and your own reverse proxy (cloudflared you run,
nginx, or anything with TLS) for the webhook port. The setup wizard
generates the proxy config for the pick: a cloudflared ingress file or an
nginx server block that forwards only `/kick/webhook` and drops the rest,
so the panel never leaks through the public hostname.

Under Docker set both listen hosts to `0.0.0.0`, or the host proxies
cannot reach the container.

## Set the public URL

Open `/settings` in Telegram and choose **Settings → Remote access**.
Paste the public URL of the proxy you run. The bot probes the URL for
reachability and saves its state to `config.json`.

## Register the URL in the Kick app

1. Open **Kick → Settings → Developer → your app → Enable webhooks**.
2. Add the webhook URL that the bot printed.

The bot prints the exact URL after each setup. The first verified event
from Kick triggers the "Kick webhook is working" confirmation.

## Test the delivery

After the entry is saved, the setup wizard offers **Test Kick delivery
now**, and Telegram has a **Test delivery** button in the Kick webhook
menu. The test temp-subscribes to the busiest live channel and waits for
its first verified event, then deletes the subscription and reports the
elapsed time. The test channel never enters your channel list: nothing
records and no chat is archived. A cold setup can take up to 3 minutes;
the usual answer arrives in seconds.

## Separate Kick entry

The panel and the control API can stay on a tailnet
while Kick delivers to its own public address. Set
`kick.webhook.public_url` to that address (the setup wizard asks for it
when a `kick:` channel is monitored). The app manages no tunnel for it:
run your own reverse proxy at the webhook listener port and paste its URL
in the Kick app. Empty follows `endpoint.public_url`, like before.

## Toggles

Remote access has one endpoint toggle: **Disable endpoint** keeps the
saved URL, and **Enable endpoint** restores the same setup without new
input. The **Kick webhook** submenu holds its own toggle, its own URL
entry, and the delivery test. **Disable Kick webhook** stops the
subscription reconcile and deletes the subscriptions of the monitored
channels. Then Kick stops the deliveries.

## Receiver internals

The receiver is `POST /kick/webhook` on
`kick.webhook.listen_host:kick.webhook.listen_port`. The app verifies every request
against the published signing key of Kick. Requests with a timestamp outside a
5-minute freshness window are rejected, so a captured request cannot be
replayed. The app deduplicates verified events by message id within that
window. Key rotation refetches are rate-limited. A per-client-IP rate limit
and a concurrency cap bound floods. Failed requests get `401` and a log entry.

The subscription sync loop reconciles the `livestream.status.updated` and
`chat.message.sent` subscriptions against the monitored Kick channels every
poll cycle. It also runs immediately on `/add`, `/remove`, `/reload`, or on
enable. If the sync fails (usually the URL is not registered in the Kick app),
the app sends one Telegram alert until the sync recovers. Webhook delivery is
best-effort: the poll covers missed live and offline events, and chat gaps stay
absent from the chat file.
