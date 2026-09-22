#!/bin/sh
# Runs the app as the data-dir owner so the host user can manage recorded files
# regardless of their uid/gid (docker-compose mounts the data dir at /data).
# USER_UID/USER_GID force a specific identity. The app never runs as root: when
# no other identity is available the entrypoint exits instead of keeping the
# image identity. The container itself starts as root, because only root can
# adopt another uid.
set -eu

if [ "$#" -eq 0 ]; then
    echo "entrypoint: no command given. Use the image CMD or pass a command." >&2
    exit 1
fi

DATA_DIR="${STREAM_ARCHIVE_DATA:-/data}"
requested_uid="${USER_UID:-}"
requested_gid="${USER_GID:-}"

# setpriv (util-linux) takes numeric ids only, and it wants them plain. Drop
# the leading zeros and bound the value to the 32-bit uid_t/gid_t range, so a
# typo fails here with a clear message instead of "setpriv: Invalid argument"
# later. Prints the normalized id.
normalize_id() {
    value="$2"
    case "$value" in
        '' | *[!0-9]*)
            echo "entrypoint: $1 must be a numeric id, got '$2'" >&2
            exit 1
            ;;
    esac
    while [ "${#value}" -gt 1 ] && [ "${value#0}" != "$value" ]; do
        value="${value#0}"
    done
    # The length check comes first: it keeps the numeric comparison of the
    # next test inside the range of the shell. (uid_t)-1 is the setpriv value
    # for "keep the current id", so 4294967295 would leave the process as root
    # without a word. The range stops below it.
    if [ "${#value}" -gt 10 ] || [ "$value" -gt 4294967294 ]; then
        echo "entrypoint: $1 is out of range (0..4294967294), got '$2'" >&2
        exit 1
    fi
    echo "$value"
}

# The exit above ends the subshell of the command substitution only, so the call
# sites check the status too: an invalid id must not leave uid empty and fall
# through to the image identity.
uid=""
gid=""
if [ -n "$requested_uid" ]; then
    uid="$(normalize_id USER_UID "$requested_uid")" || exit 1
fi
if [ -n "$requested_gid" ]; then
    gid="$(normalize_id USER_GID "$requested_gid")" || exit 1
fi

# One stat call returns both ids: two calls could read two different owners if
# the data dir changes between them.
owner_uid=""
owner_gid=""
if [ -d "$DATA_DIR" ]; then
    owner="$(stat -c '%u %g' "$DATA_DIR" 2>/dev/null)" || {
        echo "entrypoint: cannot read the owner of data dir '$DATA_DIR'" >&2
        exit 1
    }
    owner_uid="${owner%% *}"
    owner_gid="${owner##* }"
fi
if [ -z "$uid" ]; then
    uid="$owner_uid"
fi
if [ -n "$uid" ] && [ -z "$gid" ]; then
    # An explicit USER_GID wins over the data-dir group: docker-compose tells
    # the user to grant access to the tailscale socket that way. Take the
    # data-dir group only when the data dir belongs to that uid.
    if [ "$owner_uid" = "$uid" ]; then
        gid="$owner_gid"
    else
        gid="$uid"
        echo "entrypoint: USER_GID is not set and '$DATA_DIR' does not belong to uid $uid," >&2
        echo "entrypoint: so the app gets gid $uid. Set USER_GID to the group of '$DATA_DIR'." >&2
    fi
fi

# The home of the app must be writable by the app identity and private to it:
# Streamlink's plugin cache defaults to $HOME/.cache, and a world-writable home
# lets another uid pre-create that path and swap a symlink under the app. The
# rootfs is read-only, so the home lives on the /tmp tmpfs. Keep the image
# HOME=/tmp when the directory cannot be created.
app_home="${STREAM_ARCHIVE_HOME:-/tmp/stream-archive}"
if mkdir -p "$app_home" 2>/dev/null; then
    chmod 700 "$app_home" 2>/dev/null || true
    if [ "$(id -u)" = "0" ] && [ -n "$uid" ] && [ "$uid" -ne 0 ]; then
        chown "$uid:$gid" "$app_home" 2>/dev/null || true
    fi
    HOME="$app_home"
    export HOME
fi

if [ -n "$uid" ]; then
    if [ "$(id -u)" = "0" ]; then
        # The app must not run as root: it holds the bot token and the client
        # secrets, and files written by root are not manageable on the host.
        # Refuse, so the misconfiguration shows up in the container log.
        if [ "$uid" -eq 0 ]; then
            echo "entrypoint: the resolved identity is uid 0, so the app would run as root." >&2
            echo "entrypoint: chown '$DATA_DIR' to a non-root user, or set USER_UID and USER_GID." >&2
            exit 1
        fi
        if [ "$gid" -eq 0 ]; then
            echo "entrypoint: resolved gid is 0, so the app keeps the root group." >&2
            echo "entrypoint: set USER_GID to the group that owns '$DATA_DIR'." >&2
        fi

        # setpriv switches to arbitrary numeric ids without a passwd entry.
        # --clear-groups drops any supplementary groups. --no-new-privs stops a
        # setuid binary from regaining privileges. The -- separator keeps a
        # command that starts with "-" out of the setpriv option list.
        exec setpriv --no-new-privs --reuid="$uid" --regid="$gid" --clear-groups -- "$@"
    fi

    # A container that already runs as a non-root user cannot call setpriv, so
    # it keeps its identity.
    if [ "$uid" != "$(id -u)" ]; then
        echo "entrypoint: the container runs as uid $(id -u), so it cannot adopt uid $uid." >&2
        echo "entrypoint: '$DATA_DIR' must be writable by uid $(id -u)." >&2
    fi
else
    # No identity to adopt.
    if [ -n "$requested_gid" ]; then
        echo "entrypoint: USER_GID is set but USER_UID is not, so the requested gid has no effect." >&2
    fi
    if [ "$(id -u)" = "0" ]; then
        echo "entrypoint: '$DATA_DIR' is not a directory and USER_UID is not set, so there is no" >&2
        echo "entrypoint: non-root identity for the app. Create '$DATA_DIR', or set USER_UID and" >&2
        echo "entrypoint: USER_GID." >&2
        exit 1
    fi
fi

exec "$@"
