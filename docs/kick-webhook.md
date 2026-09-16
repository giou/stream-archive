# Kick webhook

The webhook gives near-instant live and offline signals, and Kick chat. The
poll alone cannot deliver chat, because Kick has no chat replay.

## Set the public URL

Open `/settings` in Telegram and choose **Remote access**. That menu sets the
public URL. It offers these options:

- **Cloudflare tunnel.** A *Quick tunnel* needs no account and gives a
  temporary URL. A *Named tunnel* takes the
  `cloudflared service install <TOKEN>` command or token, and a hostname. The
  bot writes the ingress configuration for a named tunnel. It creates the DNS
  record if you supply a Cloudflare API token, and it runs cloudflared.
- **Tailscale funnel.** The bot runs `tailscale funnel <port>`. The host must
  run tailscale. Under Docker, the app mounts the host tailscale directory
  into the container.
- **Your own tunnel.** Paste the public URL of a tunnel that you already run.

The bot probes the URL for reachability and saves its state to `config.json`.

## Register the URL in the Kick app

1. Open **Kick → Settings → Developer → your app → Enable webhooks**.
2. Add the webhook URL that the bot printed.

The bot prints the exact URL (`<endpoint>/kick/webhook`) after each tunnel
setup. The first verified event from Kick triggers the "Kick webhook is
working" confirmation.

## Toggles

Remote access has one endpoint toggle that names the action to take:
**Disable endpoint** stops the managed tunnel and keeps the saved URL and the
tunnel type. Then **Enable endpoint** restores the same setup without new
input. The **Cloudflare tunnel** and **Tailscale funnel** submenus have their
own toggle. Thus you can switch the provider or stop one tunnel without a
change to the other. The **Kick webhook** and **API** submenus hold only their
own toggle and their settings.

**Disable Kick webhook** stops the subscription reconcile and deletes the
subscriptions of the monitored channels. Then Kick stops the deliveries. A
managed Cloudflare tunnel comes back automatically after a service restart.
Its trycloudflare URL can change, and you get a new notification when it does.
The endpoint serves both features. Thus the status shows
`Endpoint: on (cloudflare · https://…)` and `Kick webhook: on` on separate
lines.

## Receiver internals

The receiver is `POST /kick/webhook` on
`endpoint.listen_host:endpoint.listen_port`. The app verifies every request
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
