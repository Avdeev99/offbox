#!/bin/bash
set -euo pipefail

container=$1
signal=$2
output=$3
errors=$4
shift 4
"$@" > "$output" 2> "$errors" &
command_pid=$!
finish() {
    if kill -0 "$command_pid" 2>/dev/null; then
        kill -KILL "$command_pid" 2>/dev/null || :
    fi
    wait "$command_pid" 2>/dev/null || :
}
trap finish EXIT
ready_deadline=$((SECONDS + 30))
while ! docker exec "$container" test -f "/work/signal-$signal.pid"; do
    if (( SECONDS >= ready_deadline )) || ! kill -0 "$command_pid" 2>/dev/null; then
        printf '%s\n' 'Smoke command did not start before its signal deadline.' >&2
        exit 1
    fi
    sleep 0.1
done
started=$(date +%s%N)
kill -s "$signal" "$command_pid"
if ! timeout 11s tail --pid="$command_pid" --sleep-interval=0.1 -f /dev/null; then
    printf '%s\n' 'Smoke command exceeded its signal grace.' >&2
    exit 1
fi
set +e
wait "$command_pid"
status=$?
set -e
elapsed=$(( ($(date +%s%N) - started) / 1000000 ))
trap - EXIT
printf '%s %s\n' "$status" "$elapsed"
