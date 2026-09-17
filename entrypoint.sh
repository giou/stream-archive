#!/bin/sh
# Runs the app as the data-dir owner so the host user can manage recorded
# files regardless of their uid/gid (docker-compose mounts the data dir at
# /data). USER_UID/USER_GID force a specific identity. Without a data dir
# (plain `docker run`) the image user is kept.
set -eu

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

uid=""
gid=""
if [ -n "$requested_uid" ]; then
    uid="$(normalize_id USER_UID "$requested_uid")"
fi
if [ -n "$requested_gid" ]; then
    gid="$(normalize_id USER_GID "$requested_gid")"
fi

if [ -z "$uid" ] && [ -d "$DATA_DIR" ]; then
    uid="$(stat -c %u "$DATA_DIR" 2>/dev/null)" || {
        echo "entrypoint: cannot read the owner of data dir '$DATA_DIR'" >&2
        exit 1
    }
    # An explicit USER_GID wins over the data-dir group: docker-compose tells
    # the user to grant access to the tailscale socket that way.
    if [ -z "$gid" ]; then
        gid="$(stat -c %g "$DATA_DIR" 2>/dev/null)" || gid="$uid"
    fi
fi

if [ -n "$uid" ]; then
    if [ -z "$gid" ]; then
        # USER_UID without USER_GID. Take the data-dir group when the data dir
        # belongs to that uid. Without that match the uid is all we know.
        if [ -d "$DATA_DIR" ] && [ "$(stat -c %u "$DATA_DIR" 2>/dev/null)" = "$uid" ]; then
            gid="$(stat -c %g "$DATA_DIR" 2>/dev/null)" || gid="$uid"
        else
            gid="$uid"
            echo "entrypoint: USER_GID is not set and '$DATA_DIR' does not belong to uid $uid," >&2
            echo "entrypoint: so the app gets gid $uid. Set USER_GID to the group of '$DATA_DIR'." >&2
        fi
    fi

    # setpriv switches to arbitrary numeric ids without a passwd entry.
    # --clear-groups drops any supplementary groups. --no-new-privs stops a
    # setuid binary from regaining privileges. The -- separator keeps a
    # command that starts with "-" out of the setpriv option list.
    if [ "$(id -u)" = "0" ]; then
        # Warn on the identity that the app runs as, not on one that a container
        # started as another user drops again.
        if [ "$uid" -eq 0 ]; then
            echo "entrypoint: resolved uid is 0, so the app runs as root." >&2
            echo "entrypoint: set USER_UID and USER_GID, or chown '$DATA_DIR' to a non-root user." >&2
        fi
        if [ "$gid" -eq 0 ] && [ "$uid" -ne 0 ]; then
            echo "entrypoint: resolved gid is 0, so the app keeps the root group." >&2
            echo "entrypoint: set USER_GID to the group that owns '$DATA_DIR'." >&2
        fi
        exec setpriv --no-new-privs --reuid="$uid" --regid="$gid" --clear-groups -- "$@"
    fi

    # A container that already runs as a non-root user cannot call setpriv, so
    # it keeps its identity.
    if [ -n "$requested_uid" ] || [ -n "$requested_gid" ]; then
        echo "entrypoint: the container already runs as uid $(id -u), so USER_UID/USER_GID are ignored." >&2
    fi
else
    # No identity to adopt. The app then writes config.json and recordings/ as
    # the image user, which is root in this image.
    echo "entrypoint: '$DATA_DIR' is not a directory and USER_UID is not set, so the app keeps the" >&2
    echo "entrypoint: image identity (uid $(id -u), gid $(id -g)). Create the directory or set USER_UID." >&2
fi

exec "$@"
