import math
import secrets
from collections.abc import Callable
from typing import Any, cast

from stream_archive.config import (
    AUDIO_ONLY_QUALITY,
    AppConfig,
    OutputMode,
    effective_quality,
    is_kick_channel,
    reload_config,
)
from stream_archive.telegram.menu_state import AudioSwitch, PendingKey, is_error

#: Button labels for the settings menus. Each table maps a label to its config
#: value. The keyboards mark the label of the current value, the handlers turn
#: a pressed label back into the config value.
MODE_CHOICES: dict[str, str] = {"Disk": "disk", "YouTube": "youtube", "Both": "both"}
QUALITY_CHOICES: dict[str, str] = {
    "Best": "best",
    "1080p": "1080p",
    "720p": "720p",
    "480p": "480p",
    "360p": "360p",
    "Audio only": "audio_only",
}
RETENTION_CHOICES: dict[str, str] = {
    "Off": "0",
    "1 day": "1",
    "3 days": "3",
    "7 days": "7",
    "14 days": "14",
    "30 days": "30",
}
COUNT_CHOICES: dict[str, str] = {"Unlimited": "0", "1": "1", "2": "2", "3": "3", "5": "5"}
DISK_SIZE_CHOICES: dict[str, str] = {"Unlimited": "0", "25": "25", "50": "50", "100": "100", "200": "200"}
HOLD_CHOICES: dict[str, str] = {"Off": "0", "30s": "30", "60s": "60", "120s": "120", "300s": "300", "600s": "600"}


#: Config keys an object built at process start owns, so a reload cannot
#: rebind them. The bot application polls with the token it was built with,
#: and the Twitch and Kick clients hold their credentials and cached tokens.
_RESTART_REQUIRED: tuple[str, ...] = (
    "bot_telegram_api",
    "twitch_client_id",
    "twitch_client_secret",
    "kick.client_id",
    "kick.client_secret",
    "youtube.privacy_status",
)


def _dotted(data: dict[str, Any], key: str) -> Any:
    """Value of a dotted config key, or None when any part is missing."""
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class SettingsCommands:
    _config: AppConfig
    _apply: Any
    _recorder: Any
    _eventsub: Any
    _kick_webhook: Any
    _admin_id: int
    #: Provided by the other mixins of the controller that composes this one.
    rebind_admin: Any
    reconcile_removed_channels: Any
    _resolve_channel_arg: Callable[..., tuple[str, str] | tuple[None, str]]
    # Maps a (chat id, nonce) pair to its quality mutation and the affected channels.
    # The admin must confirm before the change applies.
    _pending_audio_switch: dict[PendingKey, AudioSwitch]
    # ChatStateMixin drops the oldest pending prompt past its limit.
    _prune_pending: Any

    def handle_retention(self, args: list[str], chat_id: int | None = None) -> str:
        if len(args) != 1:
            return "Usage: /retention <days>"
        try:
            n = int(args[0])
        except ValueError:
            return "\u274c retention must be an integer"
        if n < 0:
            return "\u274c retention must be a non-negative integer (0 = off)"

        def mutate(candidate: AppConfig) -> None:
            candidate.retention_days = n

        return cast(str, self._apply(mutate, lambda c: f"Retention set to {n} day(s)", chat_id))

    def handle_mode(self, args: list[str], chat_id: int | None = None) -> str:
        if len(args) == 1:
            m = args[0].lower()

            def mutate(candidate: AppConfig) -> None:
                candidate.output_mode = cast(OutputMode, m)

            return cast(str, self._apply(mutate, lambda c: f"Output mode set to {m}", chat_id))

        if len(args) == 2:
            ch, err = self._resolve_channel_arg(args[0])
            if ch is None:
                return err
            m = args[1].lower()
            if m == "default":

                def mutate(candidate: AppConfig) -> None:
                    candidate.channel_output_modes.pop(ch, None)

                return cast(
                    str,
                    self._apply(mutate, lambda c: f"Output mode for {ch} reset to global ({c.output_mode})", chat_id),
                )

            def mutate(candidate: AppConfig) -> None:
                candidate.channel_output_modes[ch] = cast(OutputMode, m)

            return cast(str, self._apply(mutate, lambda c: f"Output mode for {ch} set to {m}", chat_id))

        return "Usage: /mode <disk|youtube|both> or /mode <channel> <disk|youtube|both|default>"

    def handle_channel_hold(self, args: list[str], chat_id: int | None = None) -> str:
        if len(args) != 2:
            return "Usage: /channelhold <channel> <seconds|default>"
        ch, err = self._resolve_channel_arg(args[0])
        if ch is None:
            return err
        if args[1] == "default":

            def mutate(candidate: AppConfig) -> None:
                candidate.channel_youtube_hold_seconds.pop(ch, None)

            return cast(
                str,
                self._apply(
                    mutate, lambda c: f"Hold delay for {ch} reset to global ({c.youtube.hold_seconds:g}s)", chat_id
                ),
            )

        try:
            n = int(args[1])
        except ValueError:
            return "\u274c hold delay must be a non-negative integer (seconds) or 'default'"
        if n < 0:
            return "\u274c hold delay must be a non-negative integer (seconds) or 'default'"

        def set_hold(candidate: AppConfig) -> None:
            candidate.channel_youtube_hold_seconds[ch] = n

        return cast(
            str, self._apply(set_hold, lambda c: f"Hold delay for {ch} set to {n}s (0 = end immediately)", chat_id)
        )

    def _audio_conflicts(self, candidate: AppConfig) -> list[str]:
        """Channels that would record audio-only into YouTube after this change."""
        return sorted(
            ch
            for ch in candidate.channels
            if effective_quality(candidate, ch) == AUDIO_ONLY_QUALITY
            and candidate.channel_output_modes.get(ch, candidate.output_mode) != "disk"
        )

    def _probe_quality_change(self, mutate: Callable[[AppConfig], Any]) -> tuple[list[str], BaseException | None]:
        """Run a quality change on a temporary copy without saving.

        Returns the conflicting channels, or the validation error for an
        invalid value. Nothing touches config.json.
        """
        probe = self._config.model_copy(deep=True)
        try:
            mutate(probe)
        except ValueError as e:
            return [], e
        return self._audio_conflicts(probe), None

    def _gate_quality(
        self, mutate: Callable[[AppConfig], Any], conflicts: list[str], chat_id: int | None = None
    ) -> str | None:
        """Stores a pending audio-only switch when the change creates a conflict."""
        if not conflicts:
            return None
        nonce = secrets.token_hex(4)
        chat = chat_id if chat_id is not None else self._admin_id
        self._pending_audio_switch[(chat, nonce)] = (mutate, conflicts)
        self._prune_pending(self._pending_audio_switch, chat)
        return f"\u26a0\ufe0f Setting audio_only quality will set output mode to disk for: {', '.join(conflicts)}"

    def _apply_quality(self, mutate: Callable[[AppConfig], Any], ok_text: str, chat_id: int | None) -> str:
        """Save one quality change, or hold it for a confirm when it conflicts.

        A change to audio_only can force a recording onto disk, and that
        switch needs the confirm of the admin.
        """
        conflicts, err = self._probe_quality_change(mutate)
        if err is not None:
            return f"\u274c {err}"
        gated = self._gate_quality(mutate, conflicts, chat_id)
        if gated is not None:
            return gated
        return cast(str, self._apply(mutate, lambda _candidate: ok_text, chat_id))

    def handle_quality(self, args: list[str], chat_id: int | None = None) -> str:
        c = self._config
        if not args:
            text = f"Quality: {c.preferred_quality}"
            if c.channel_preferred_qualities:
                text += "\nPer-channel: " + ", ".join(
                    f"{ch} \u2192 {q}" for ch, q in sorted(c.channel_preferred_qualities.items())
                )
            return text
        if len(args) == 1:
            q = args[0].lower()

            def mutate(candidate: AppConfig) -> None:
                candidate.preferred_quality = q

            return self._apply_quality(mutate, f"Quality set to {q}", chat_id)
        if len(args) == 2:
            ch, err = self._resolve_channel_arg(args[0])
            if ch is None:
                return err
            q = args[1].lower()
            if q == "default":

                def mutate(candidate: AppConfig) -> None:
                    candidate.channel_preferred_qualities.pop(ch, None)

                # Resetting to global can create a conflict when the global
                # quality is audio_only, so this path runs through the same gate.
                return self._apply_quality(mutate, f"Quality for {ch} reset to global ({c.preferred_quality})", chat_id)

            def mutate(candidate: AppConfig) -> None:
                candidate.channel_preferred_qualities[ch] = q

            return self._apply_quality(mutate, f"Quality for {ch} set to {q}", chat_id)
        return "Usage: /quality <best|1080p|720p|...> or /quality <channel> <quality|default>"

    def _set_count(self, field: str, label: str, usage: str, args: list[str], chat_id: int | None) -> str:
        """Show or set one concurrency count of the config.

        The error reply uses ``label`` with a lower-case first letter.
        """
        if not args:
            return f"{label}: {getattr(self._config, field)} (0 = unlimited)"
        if len(args) != 1:
            return usage
        try:
            n = int(args[0])
        except ValueError:
            return f"\u274c {label[:1].lower()}{label[1:]} must be an integer"
        if n < 0:
            return f"\u274c {label[:1].lower()}{label[1:]} must be a non-negative integer"
        return cast(
            str,
            self._apply(
                lambda candidate: setattr(candidate, field, n),
                lambda candidate: f"{label} set to {n}",
                chat_id,
            ),
        )

    def handle_maxrecordings(self, args: list[str], chat_id: int | None = None) -> str:
        return self._set_count(
            "max_concurrent_recordings", "Max recordings", "Usage: /maxrecordings <n> (0 = unlimited)", args, chat_id
        )

    def handle_maxyoutube(self, args: list[str], chat_id: int | None = None) -> str:
        return self._set_count(
            "max_concurrent_youtube_streams",
            "Max YouTube re-streams",
            "Usage: /maxyoutube <n> (0 = unlimited)",
            args,
            chat_id,
        )

    def handle_disk(self, args: list[str], chat_id: int | None = None) -> str:
        c = self._config
        usage = "Usage: /disk <maxsize|delete_oldest> <value>"
        if not args:
            d = c.disk
            return (
                "Disk limits:\n"
                f"max total: {d.max_total_gb:g} GB (0 = disabled, delete oldest: {'on' if d.delete_oldest else 'off'})"
            )
        if len(args) != 2:
            return usage
        cmd, val = args[0].lower(), args[1]
        if cmd == "delete_oldest":
            if val == "on":
                return cast(
                    str,
                    self._apply(
                        lambda candidate: setattr(candidate.disk, "delete_oldest", True),
                        lambda candidate: "Delete oldest enabled",
                        chat_id,
                    ),
                )
            if val == "off":
                return cast(
                    str,
                    self._apply(
                        lambda candidate: setattr(candidate.disk, "delete_oldest", False),
                        lambda candidate: "Delete oldest disabled",
                        chat_id,
                    ),
                )
            return usage
        try:
            v = float(val)
        except ValueError:
            return f"\u274c {cmd} must be a number"
        if not math.isfinite(v) or v < 0:
            # A NaN or an infinity would persist and then silently disable
            # every later "used > limit" check of the disk limiter.
            return f"\u274c {cmd} must be a non-negative number"
        if cmd == "maxsize":
            return cast(
                str,
                self._apply(
                    lambda candidate: setattr(candidate.disk, "max_total_gb", v),
                    lambda candidate: f"Disk max total set to {v:g} GB",
                    chat_id,
                ),
            )
        return usage

    async def handle_chat(self, args: list[str], chat_id: int | None = None) -> str:
        if not args:
            twitch_state = "enabled" if self._config.record_chat else "disabled"
            kick_state = "enabled" if self._config.kick.record_chat else "disabled"
            return f"Chat recording: {twitch_state}\nKick chat recording: {kick_state}"
        if len(args) == 1 and args[0].lower() in ("on", "off"):
            enabled = args[0].lower() == "on"

            def mutate(candidate: AppConfig) -> None:
                candidate.record_chat = enabled
                candidate.kick.record_chat = enabled

            text: str = self._apply(
                mutate,
                lambda candidate: f"Chat recording {'enabled' if enabled else 'disabled'}",
                chat_id,
            )
            if not enabled and not is_error(text):
                for channel in self._recorder.active_channels():
                    await self._recorder.stop_chat(channel)
            return text
        if len(args) == 2 and args[0].lower() in ("on", "off") and args[1].lower() in ("twitch", "kick"):
            enabled = args[0].lower() == "on"
            platform = args[1].lower()

            def mutate(candidate: AppConfig) -> None:
                if platform == "twitch":
                    candidate.record_chat = enabled
                else:
                    candidate.kick.record_chat = enabled

            label = "Twitch chat recording" if platform == "twitch" else "Kick chat recording"
            text = self._apply(
                mutate,
                lambda candidate: f"{label} {'enabled' if enabled else 'disabled'}",
                chat_id,
            )
            if not enabled and not is_error(text):
                for channel in self._recorder.active_channels():
                    if platform == "twitch" and not is_kick_channel(channel):
                        await self._recorder.stop_chat(channel, "twitch")
                    elif platform == "kick" and is_kick_channel(channel):
                        await self._recorder.stop_chat(channel, "kick")
            return text
        return "Usage: /chat <on|off> [twitch|kick]"

    async def handle_reload(self) -> str:
        before_channels = list(self._config.channels)
        before = self._config.model_dump()
        try:
            reload_config(self._config)
        except ValueError as e:
            return f"\u274c Reload failed: {e}"
        # The admin gate, the callback gate and the alert target were built
        # from the startup config, so a changed telegram_user_id must reach
        # them here; otherwise the previous identity keeps every operation.
        self.rebind_admin()
        notes: list[str] = []
        try:
            notes = await self.reconcile_removed_channels(
                [ch for ch in before_channels if ch not in self._config.channels]
            )
            await self._eventsub.sync_channels(self._config.channels)
            if self._kick_webhook:
                # The listener serves the endpoint and the control API. This call
                # applies a changed endpoint or webhook state, a listener address,
                # and a changed API state from the reloaded file.
                await self._kick_webhook.apply_state()
                await self._kick_webhook.sync_channels(self._config.channels)
            mtproto = getattr(self, "_mtproto", None)
            if mtproto is None and self._config.mtproto.enabled:
                notes.append(
                    "\u26a0\ufe0f MTProto enabled in the file, but no client exists \u2014 restart to create it."
                )
            if mtproto is not None:
                if self._config.mtproto.enabled:
                    try:
                        await mtproto.connect()
                    except Exception:
                        notes.append("\u26a0\ufe0f MTProto login failed \u2014 check the logs.")
                else:
                    await mtproto.rebind()
        except Exception as e:
            # The file is reloaded already, so report the failure instead of
            # leaving the admin without a reply and the state out of sync. A
            # channel that was already released is still reported: it is gone
            # from the file, so a retry cannot report it again.
            detail = f"\u26a0\ufe0f Config reloaded, but applying it failed: {e}"
            if notes:
                detail += "\n" + "\n".join(notes)
            return detail
        # Some values are held by an object the process built at startup and
        # cannot be swapped in place: the bot application polls with its own
        # token, and the API clients hold their credentials and cached tokens.
        after = self._config.model_dump()
        stale = [key for key in _RESTART_REQUIRED if _dotted(before, key) != _dotted(after, key)]
        if stale:
            return (
                "\u26a0\ufe0f Config reloaded, but these keys need a restart: "
                + ", ".join(stale)
                + ("\n" + "\n".join(notes) if notes else "")
            )
        if notes:
            return "\u2705 Config reloaded from config.json\n" + "\n".join(notes)
        return "\u2705 Config reloaded from config.json"
