# Failure handling

The app logs each failure, sends an alert, and retries on the next poll cycle.
The table that follows lists the behavior for each failure type.

| Failure | Behavior |
| --- | --- |
| All ad-block proxies fail for a live Twitch channel | Channel skipped. One alert (rate-limited to 30 minutes for each channel). Retry on the next cycle |
| Kick blocks recording requests (anti-bot challenge or `403`) | Channel skipped. One alert (rate-limited) with a hint to install a browser on the host. Retry on the next cycle |
| YouTube rate limit, `403`, or quota error at broadcast creation | Automatic fallback to disk recording. The live alert still goes out |
| Other YouTube broadcast-creation error | The task fails with an error. Restart on the next cycle |
| Recording dies mid-stream (ffmpeg killed, disk write error, proxy death, stalled feed) | Entry removed (chat and broadcast finalized). Restart on the next cycle. No alert if the recovery succeeds. A rate-limited alert only if the restart also fails |
| YouTube re-stream keeps ending shortly after start (flaky feed) | Restart delays grow for each channel. They double from 120 s to a cap of 30 min. A rolling 24-hour budget of 10 broadcast creations across all channels blocks further restarts until a slot frees (one alert with the next-slot time). This budget guards the daily broadcast limit of YouTube (`userBroadcastsExceedLimit`) |
| Source drops briefly with a hold delay configured | The broadcast stays open, fed by a bundled pre-encoded "Reconnecting..." clip. A return within the delay reuses the same broadcast (no new creation, no quota cost). On expiry the broadcast ends as usual |
| Transient Twitch or Kick API error (token or request) | Logged. The app takes no action. Retry on the next cycle |
| Kick channel slug not found | Warned once for each channel. Treated as offline. The poll never crashes |
| EventSub connection lost or conduit shard disabled | Auto-reconnect with backoff. The shard re-associates with the new session. The poll covers the missed events |
| Kick webhook signature verification failed | The app answers `401` and logs the request. Valid requests keep flowing |
| Kick webhook subscription sync fails (URL not registered in the Kick app) | One alert until the sync recovers. Polling still covers live and offline |
| Stream reported for an unknown user id | Skipped with a warning. The poll never crashes |

Alerts are sent at most once per 30 minutes for each channel
(`FAILURE_NOTIFY_INTERVAL` in `src/stream_archive/monitor.py`).

## Related guides

- [Kick webhook](kick-webhook.md) covers the subscription sync and the
  signature check.
- [Telegram control](telegram-control.md) explains the alerts and the
  `/status` command.
