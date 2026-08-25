#!/bin/sh
# Runs the app as the data-dir owner so the host user can manage recorded
# files regardless of their uid/gid (docker-compose mounts the data dir at
# /data). USER_UID/USER_GID force a specific identity. Without a data dir
# (plain `docker run`) the image user is kept.
set -eu

DATA_DIR="${STREAM_ARCHIVE_DATA:-/data}"
uid="${USER_UID:-}"
gid="${USER_GID:-}"

if [ -z "$uid" ] && [ -d "$DATA_DIR" ]; then
    uid="$(stat -c %u "$DATA_DIR")"
    gid="$(stat -c %g "$DATA_DIR")"
fi

if [ -n "$uid" ]; then
    [ -n "$gid" ] || gid="$uid"
    # setpriv (util-linux) switches to arbitrary numeric ids without a
    # passwd entry. --clear-groups drops any supplementary groups.
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
fi

exec "$@"
