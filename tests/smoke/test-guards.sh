#!/bin/bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
mkdir "$scratch/bin" "$scratch/runner"
export RUNNER_TEMP=$scratch/runner
export PATH=$scratch/bin:$PATH
export SMOKE_GUARD_CALLS=$scratch/calls
cat > "$scratch/bin/uname" <<'STUB'
#!/bin/sh
case "$1" in -s) printf 'Linux\n' ;; -m) printf 'x86_64\n' ;; *) exit 9 ;; esac
STUB
cat > "$scratch/bin/docker" <<'STUB'
#!/bin/sh
printf 'docker %s\n' "$*" >> "$SMOKE_GUARD_CALLS"
[ "${SMOKE_GUARD_INVENTORY_FAIL:-0}" = 0 ] || exit 9
case "$*" in
    'network ls -q '*)
        if [ "${SMOKE_GUARD_NETWORK_INSPECT_FAIL:-0}" = 1 ]; then printf 'guard-network\n'; fi
        exit 0 ;;
    'ps -aq '*|'volume ls -q '*) exit 0 ;;
    *) exit 9 ;;
esac
STUB
cat > "$scratch/bin/sudo" <<'STUB'
#!/bin/sh
printf 'sudo %s\n' "$*" >> "$SMOKE_GUARD_CALLS"
[ "${SMOKE_GUARD_DELETE_FAIL:-0}" = 0 ] || exit 9
[ "$#" -eq 4 ] && [ "$1" = rm ] && [ "$2" = -rf ] && [ "$3" = -- ] && [ "$4" = "${SMOKE_GUARD_ALLOWED_KEYS:?}" ] || exit 9
exec /bin/rm -rf -- "$4"
STUB
chmod +x "$scratch/bin/"*

expect_status() {
    local expected=$1 actual
    shift
    if "$@"; then actual=0; else actual=$?; fi
    if [[ "$actual" != "$expected" ]]; then
        printf 'Expected exit%s, received%s: %s\n' "$expected" "$actual" "$*" >&2
        exit 1
    fi
}

workspace=$RUNNER_TEMP/offbox-smoke-guard-existing
mkdir -p "$workspace/keys"
printf 'unrelated\n' > "$workspace/keys/sentinel"
expect_status 3 "$script_dir/run.sh" run guard-existing unused-image
expect_status 1 "$script_dir/run.sh" cleanup guard-existing
test "$(cat "$workspace/keys/sentinel")" = unrelated
while IFS= read -r call; do
    [[ "$call" != sudo* ]] || exit 1
done < "$SMOKE_GUARD_CALLS"

mkdir -p "$scratch/unrelated/keys"
printf 'unrelated\n' > "$scratch/unrelated/keys/sentinel"
ln -s "$scratch/unrelated" "$RUNNER_TEMP/offbox-smoke-guard-symlink"
expect_status 3 "$script_dir/run.sh" run guard-symlink unused-image
expect_status 1 "$script_dir/run.sh" cleanup guard-symlink
test "$(cat "$scratch/unrelated/keys/sentinel")" = unrelated

workspace=$RUNNER_TEMP/offbox-smoke-guard-owned
mkdir -p "$workspace/artifacts"
printf 'guard-owned\n' > "$workspace/.offbox-smoke-owner"
ln -s "$scratch/unrelated/keys" "$workspace/keys"
expect_status 1 "$script_dir/run.sh" cleanup guard-owned
test "$(cat "$scratch/unrelated/keys/sentinel")" = unrelated
rm -- "$workspace/keys"
mkdir "$workspace/keys"
printf 'owned\n' > "$workspace/keys/sentinel"
export SMOKE_GUARD_ALLOWED_KEYS=$workspace/keys
export SMOKE_GUARD_DELETE_FAIL=1
expect_status 1 "$script_dir/run.sh" cleanup guard-owned
test "$(cat "$workspace/keys/sentinel")" = owned
export SMOKE_GUARD_DELETE_FAIL=0
export SMOKE_GUARD_INVENTORY_FAIL=1
expect_status 1 "$script_dir/run.sh" cleanup guard-owned
test ! -e "$workspace/keys"
export SMOKE_GUARD_INVENTORY_FAIL=0
expect_status 0 "$script_dir/run.sh" cleanup guard-owned
export SMOKE_GUARD_NETWORK_INSPECT_FAIL=1
expect_status 1 "$script_dir/run.sh" cleanup guard-owned
export SMOKE_GUARD_NETWORK_INSPECT_FAIL=0
printf '{invalid-json' > "$workspace/artifacts/manifest.json"
expect_status 1 "$script_dir/run.sh" cleanup guard-owned
printf '{"envs": []}\n' > "$workspace/artifacts/manifest.json"
expect_status 0 "$script_dir/run.sh" cleanup guard-owned
printf '%s\n' 'Smoke ownership/symlink/deletion/inventory guards passed without Docker.'
