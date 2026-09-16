#!/bin/sh
# Runs the app as the data-dir owner so the host user can manage recorded
# files regardless of their uid/gid (docker-compose mounts the data dir at
# /data). USER_UID/USER_GID force a specific identity. Without a data dir
# (plain `docker run`) the image user is kept.
set -eu

DATA_DIR="${STREAM_ARCHIVE_DATA:-/data}"
uid="${USER_UID:-}"
gid="${USER_GID:-}"

# setpriv (util-linux) takes numeric ids only. Reject a bad value here, so a
# typo fails with a clear message instead of "setpriv: Invalid argument".
check_id() {
    case "$2" in
        '' | *[!0-9]*)
            echo "entrypoint: $1 must be a numeric id, got '$2'" >&2
            exit 1
            ;;
    esac
}

if [ -n "$uid" ]; then
    check_id USER_UID "$uid"
fi
if [ -n "$gid" ]; then
    check_id USER_GID "$gid"
fi

if [ -z "$uid" ] && [ -d "$DATA_DIR" ]; then
    uid="$(stat -c %u "$DATA_DIR" 2>/dev/null)" || {
        echo "entrypoint: cannot read the owner of data dir '$DATA_DIR'" >&2
        exit 1
    }
    gid="$(stat -c %g "$DATA_DIR" 2>/dev/null)" || gid="$uid"
fi

if [ -n "$uid" ]; then
    # USER_GID is optional. Fall back to the uid. The block above took the
    # data-dir group together with the data-dir uid, so a root-owned data dir
    # never hands the process the root group here.
    if [ -z "$gid" ]; then
        gid="$uid"
    fi
    if [ "$uid" = "0" ]; then
        echo "entrypoint: resolved uid is 0, so the app runs as root." >&2
        echo "entrypoint: set USER_UID and USER_GID, or chown '$DATA_DIR' to a non-root user." >&2
    fi
    if [ "$gid" = "0" ] && [ "$uid" != "0" ]; then
        echo "entrypoint: resolved gid is 0, so the app keeps the root group." >&2
        echo "entrypoint: set USER_GID to the group that owns '$DATA_DIR'." >&2
    fi
    # setpriv switches to arbitrary numeric ids without a passwd entry.
    # --clear-groups drops any supplementary groups. --no-new-privs stops a
    # setuid binary from regaining privileges. A container that already runs
    # as a non-root user cannot call setpriv, so it keeps its identity.
    # The -- separator keeps a command that starts with "-" out of the
    # setpriv option list.
    if [ "$(id -u)" = "0" ]; then
        exec setpriv --no-new-privs --reuid="$uid" --regid="$gid" --clear-groups -- "$@"
    fi
fi

exec "$@"
