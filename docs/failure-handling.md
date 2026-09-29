# Failure handling

The app logs each failure, sends an alert, and retries on the next poll cycle. The table that follows lists the behavior for each failure type.

| Failure | Behavior |
| --- | --- |
| All ad-block proxies fail for a live Twitch channel | Channel skipped. One alert (rate-limited to 30 minutes for each channel). Retry on the next cycle |
| Kick blocks recording requests (anti-bot challenge or `403`) | Channel skipped. One alert (rate-limited) with a hint to install a browser on the host. Retry on the next cycle |
| YouTube rate limit, `403`, or quota error at broadcast creation | Automatic fallback to disk recording. The live alert still goes out |
| Other YouTube broadcast-creation error | The task fails with an error. Restart on the next cycle |
| Recording dies mid-stream (ffmpeg killed, disk write error, proxy death, stalled feed) | Entry removed (chat and broadcast finalized). Restart on the next cycle. No alert if the recovery succeeds. A rate-limited alert only if the restart also fails |
| YouTube restream keeps ending shortly after start (flaky feed) | Restart delays grow for each channel. They double from 120 s to a cap of 30 min. A rolling 24-hour budget of 10 broadcast creations across all channels blocks further restarts until a slot frees (one alert with the next-slot time) |
| Source drops briefly with a hold delay set | The broadcast stays open, fed by a bundled pre-encoded "Reconnecting..." clip. A return within the delay reuses the same broadcast (no new creation, no quota cost). On expiry the broadcast ends as usual |
| Transient Twitch or Kick API error (token or request) | Logged. The app takes no action. Retry on the next cycle |
| Twitch or Kick rejects the app credentials (HTTP 400/401/403) | One alert until calls succeed again. Monitoring stays stalled. `/readyz` and `/status` name it `twitch_auth` or `kick_auth` |
| YouTube token dead or broken | One alert naming `stream-archive-setup-youtube`. Restreaming stays stalled. `/readyz` and `/status` name it `youtube_auth` |
| Archive disk under 1 GB free | No recording starts. One alert for each channel until space returns. `/readyz` and `/status` name it `disk_full` |
| Kick channel slug not found | Warned once for each channel. Treated as offline. The poll never crashes |
| EventSub connection lost or conduit shard disabled | Auto-reconnect with backoff. The shard re-associates with the new session. The poll covers the missed events |
| Kick webhook signature check failed | The app answers `401` and logs the request. Valid requests keep flowing |
| Kick webhook rate limit hit | The app answers `429` with `too many requests`. Kick retries later. The poll covers the gap |
| Kick webhook body too large or too slow | The app answers `413`. Kick retries later. The poll covers the gap |
| Kick webhook signing key unavailable | The app answers `503` with `Retry-After: 5`, so Kick redelivers. A busy app answers `503` with `Retry-After: 1` |
| Kick webhook unknown event type | The app answers `204` and logs the type. Nothing else happens |
| Kick webhook bad body (bad encoding or truncated JSON) | The app answers `400`, so Kick retries. The retry dispatches as new, not as a replay |
| Kick webhook duplicate delivery | The app answers `200` and ignores the replay. A failed dispatch rolls back the dedup mark, so the retry dispatches as new |
| Kick webhook subscription sync fails (URL not registered in the Kick app) | One alert until the sync recovers. Polling still covers live and offline |
| Stream reported for an unknown user id | Skipped with a warning. The poll never crashes |
| Disk cap reached with `disk.delete_oldest: false` | New recordings stop with one alert for each channel. Old files stay. Set `delete_oldest: true` or free disk space |
| Retention sweep fails | Logged with the cause. The next sweep retries in 1 hour, not on the next poll cycle |
| MTProto upload or login fails | Logged with the cause. The Telegram bot replies with a failure note. Nothing retries on its own |
| Update check fails (network error) | Logged with the cause. The status reads `unknown`. No alert goes out. The next check retries on schedule |
| Health endpoint bind fails | Logged with the cause. The app runs without the healthcheck endpoint |

Alerts arrive at most once per 30 minutes for each channel (`FAILURE_NOTIFY_INTERVAL` in `src/stream_archive/monitor.py`).

## Related guides

- [Kick webhook](kick-webhook.md) covers the subscription sync and the signature check.
- [Telegram control](telegram-control.md) explains the alerts and the `/status` command.
