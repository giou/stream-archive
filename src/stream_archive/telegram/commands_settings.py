import secrets
from collections.abc import Callable
from typing import Any, cast

from stream_archive.config import (
    AUDIO_ONLY_QUALITY,
    AppConfig,
    OutputMode,
    effective_quality,
    is_kick_channel,
    normalize_channel_name,
    reload_config,
)

_QUALITY_PRESETS = ("best", "1080p", "720p", "480p", "360p", "audio_only")


class SettingsCommands:
    _config: AppConfig
    _apply: Any
    _recorder: Any
    _eventsub: Any
    _kick_webhook: Any
    # nonce -> (quality mutation, affected channels) awaiting the admin's choice.
    _pending_audio_switch: dict[str, tuple[Callable[[AppConfig], Any], list[str]]]

    def handle_retention(self, args: list[str]) -> str:
        if len(args) != 1:
            return "Usage: /retention <days>"
        try:
            n = int(args[0])
        except ValueError:
            return "\u274c retention must be an integer"

        def mutate(candidate: AppConfig) -> None:
            candidate.retention_days = n

        return self._apply(mutate, lambda c: f"Retention set to {n} day(s)")

    def handle_mode(self, args: list[str]) -> str:
        if len(args) == 1:
            m = args[0].lower()

            def mutate(candidate: AppConfig) -> None:
                candidate.output_mode = cast(OutputMode, m)

            return self._apply(mutate, lambda c: f"Output mode set to {m}")

        if len(args) == 2:
            ch, m = args[0], args[1].lower()
            normalized = normalize_channel_name(ch)
            if normalized is None:
                return f"\u274c Invalid channel name: {ch!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"
            ch = normalized
            if m == "default":

                def mutate(candidate: AppConfig) -> None:
                    candidate.channel_output_modes.pop(ch, None)

                return self._apply(mutate, lambda c: f"Output mode for {ch} reset to global ({c.output_mode})")

            def mutate(candidate: AppConfig) -> None:
                candidate.channel_output_modes[ch] = cast(OutputMode, m)

            return self._apply(mutate, lambda c: f"Output mode for {ch} set to {m}")

        return "Usage: /mode <disk|youtube|both> or /mode <channel> <disk|youtube|both|default>"

    def handle_channel_hold(self, args: list[str]) -> str:
        if len(args) != 2:
            return "Usage: /channelhold <channel> <seconds|default>"
        ch = normalize_channel_name(args[0])
        if ch is None:
            return f"\u274c Invalid channel name: {ch!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"
        if args[1] == "default":

            def mutate(candidate: AppConfig) -> None:
                candidate.channel_youtube_hold_seconds.pop(ch, None)

            return self._apply(mutate, lambda c: f"Hold delay for {ch} reset to global ({c.youtube.hold_seconds:g}s)")

        try:
            n = int(args[1])
        except ValueError:
            return "\u274c hold delay must be a non-negative integer (seconds) or 'default'"
        if n < 0:
            return "\u274c hold delay must be a non-negative integer (seconds) or 'default'"

        def set_hold(candidate: AppConfig) -> None:
            candidate.channel_youtube_hold_seconds[ch] = n

        return self._apply(set_hold, lambda c: f"Hold delay for {ch} set to {n}s (0 = end immediately)")

    def _audio_conflicts(self, candidate: AppConfig) -> list[str]:
        """Channels that would record audio-only into YouTube after this change."""
        return sorted(
            ch
            for ch in candidate.channels
            if effective_quality(candidate, ch) == AUDIO_ONLY_QUALITY
            and candidate.channel_output_modes.get(ch, candidate.output_mode) != "disk"
        )

    def _probe_quality_change(self, mutate: Callable[[AppConfig], Any]) -> tuple[list[str], BaseException | None]:
        """Run a quality mutation on a throwaway copy without saving.

        Returns the conflicting channels, or the validation error from an
        invalid value. Nothing touches config.json.
        """
        probe = self._config.model_copy(deep=True)
        try:
            mutate(probe)
        except ValueError as e:
            return [], e
        return self._audio_conflicts(probe), None

    def _gate_quality(self, mutate: Callable[[AppConfig], Any], conflicts: list[str]) -> str | None:
        """Stash a pending audio-only switch when the change would conflict."""
        if not conflicts:
            return None
        nonce = secrets.token_hex(4)
        self._pending_audio_switch[nonce] = (mutate, conflicts)
        return f"\u26a0\ufe0f Setting audio_only quality will set output mode to disk for: {', '.join(conflicts)}"

    def handle_quality(self, args: list[str]) -> str:
        c = self._config
        if not args:
            text = f"Quality: {c.preferred_quality}"
            if c.channel_preferred_qualities:
                text += "\nPer-channel: " + ", ".join(
                    f"{ch} \u2192 {q}" for ch, q in sorted(c.channel_preferred_qualities.items())
                )
            return text
        if len(args) == 1:
            q = args[0]

            def mutate(candidate: AppConfig) -> None:
                candidate.preferred_quality = q

            conflicts, err = self._probe_quality_change(mutate)
            if err is not None:
                return f"\u274c {err}"
            gated = self._gate_quality(mutate, conflicts)
            if gated is not None:
                return gated
            return self._apply(mutate, lambda candidate: f"Quality set to {q}")
        if len(args) == 2:
            normalized = normalize_channel_name(args[0])
            if normalized is None:
                return (
                    f"\u274c Invalid channel name: {args[0]!r} (use twitch:<name> for Twitch or kick:<name> for Kick)"
                )
            ch, q = normalized, args[1].lower()
            if q == "default":

                def mutate(candidate: AppConfig) -> None:
                    candidate.channel_preferred_qualities.pop(ch, None)

                # Resetting to global can create a conflict when the global
                # quality is audio_only, so this path runs through the same gate.
                conflicts, err = self._probe_quality_change(mutate)
                if err is not None:
                    return f"\u274c {err}"
                gated = self._gate_quality(mutate, conflicts)
                if gated is not None:
                    return gated
                return self._apply(
                    mutate, lambda candidate: f"Quality for {ch} reset to global ({candidate.preferred_quality})"
                )

            def mutate(candidate: AppConfig) -> None:
                candidate.channel_preferred_qualities[ch] = q

            conflicts, err = self._probe_quality_change(mutate)
            if err is not None:
                return f"\u274c {err}"
            gated = self._gate_quality(mutate, conflicts)
            if gated is not None:
                return gated
            return self._apply(mutate, lambda candidate: f"Quality for {ch} set to {q}")
        return "Usage: /quality <best|1080p|720p|...> or /quality <channel> <quality|default>"

    def handle_maxrecordings(self, args: list[str]) -> str:
        if not args:
            return f"Max recordings: {self._config.max_concurrent_recordings} (0 = unlimited)"
        if len(args) == 1:
            try:
                n = int(args[0])
            except ValueError:
                return "\u274c max recordings must be an integer"
            return self._apply(
                lambda candidate: setattr(candidate, "max_concurrent_recordings", n),
                lambda candidate: f"Max recordings set to {n}",
            )
        return "Usage: /maxrecordings <n> (0 = unlimited)"

    def handle_maxyoutube(self, args: list[str]) -> str:
        if not args:
            return f"Max YouTube re-streams: {self._config.max_concurrent_youtube_streams} (0 = unlimited)"
        if len(args) == 1:
            try:
                n = int(args[0])
            except ValueError:
                return "\u274c max YouTube re-streams must be an integer"
            return self._apply(
                lambda candidate: setattr(candidate, "max_concurrent_youtube_streams", n),
                lambda candidate: f"Max YouTube re-streams set to {n}",
            )
        return "Usage: /maxyoutube <n> (0 = unlimited)"

    def handle_disk(self, args: list[str]) -> str:
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
                return self._apply(
                    lambda candidate: setattr(candidate.disk, "delete_oldest", True),
                    lambda candidate: "Delete oldest enabled",
                )
            if val == "off":
                return self._apply(
                    lambda candidate: setattr(candidate.disk, "delete_oldest", False),
                    lambda candidate: "Delete oldest disabled",
                )
            return usage
        try:
            v = float(val)
        except ValueError:
            return f"\u274c {cmd} must be a number"
        if cmd == "maxsize":
            return self._apply(
                lambda candidate: setattr(candidate.disk, "max_total_gb", v),
                lambda candidate: f"Disk max total set to {v:g} GB",
            )
        return usage

    async def handle_chat(self, args: list[str]) -> str:
        if not args:
            twitch_state = "enabled" if self._config.record_chat else "disabled"
            kick_state = "enabled" if self._config.kick.record_chat else "disabled"
            return f"Chat recording: {twitch_state}\nKick chat recording: {kick_state}"
        if len(args) == 1 and args[0].lower() in ("on", "off"):
            enabled = args[0].lower() == "on"

            def mutate(candidate: AppConfig) -> None:
                candidate.record_chat = enabled
                candidate.kick.record_chat = enabled

            text = self._apply(
                mutate,
                lambda candidate: f"Chat recording {'enabled' if enabled else 'disabled'}",
            )
            if not enabled and not text.startswith("\u274c"):
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
            )
            if not enabled and not text.startswith("\u274c"):
                for channel in self._recorder.active_channels():
                    if platform == "twitch" and not is_kick_channel(channel):
                        await self._recorder.stop_chat(channel, "twitch")
                    elif platform == "kick" and is_kick_channel(channel):
                        await self._recorder.stop_chat(channel, "kick")
            return text
        return "Usage: /chat <on|off> [twitch|kick]"

    async def handle_reload(self) -> str:
        try:
            reload_config(self._config)
        except ValueError as e:
            return f"\u274c Reload failed: {e}"
        await self._eventsub.sync_channels(self._config.channels)
        if self._kick_webhook:
            await self._kick_webhook.sync_channels(self._config.channels)
        return "\u2705 Config reloaded from config.json"
