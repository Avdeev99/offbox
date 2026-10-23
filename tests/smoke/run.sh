#!/bin/bash
set -euo pipefail

mode=${1:?Expected run or cleanup}
fixture_id=${2:?Expected a unique fixture id}
case "$fixture_id" in ''|*[!a-z0-9-]*) printf '%s\n' 'Invalid smoke fixture id.' >&2; exit 2 ;; esac
task_dir=${RUNNER_TEMP:?}/offbox-smoke-$fixture_id
label=offbox.smoke=$fixture_id

workspace_owned() {
    [[ -d "$task_dir" && ! -L "$task_dir" && -O "$task_dir" ]] &&
        [[ -f "$task_dir/.offbox-smoke-owner" && ! -L "$task_dir/.offbox-smoke-owner" && -O "$task_dir/.offbox-smoke-owner" ]] &&
        [[ -d "$task_dir/artifacts" && ! -L "$task_dir/artifacts" && ! -L "$task_dir/keys" ]] &&
        [[ $(cat "$task_dir/.offbox-smoke-owner") == "$fixture_id" ]]
}

srv_owned() {
    [[ ! -L /srv && ! -L /srv/offbox && -d /srv/offbox/envs && ! -L /srv/offbox/envs ]] &&
        [[ -f /srv/offbox/envs/.offbox-smoke-owner && ! -L /srv/offbox/envs/.offbox-smoke-owner ]] &&
        [[ $(cat /srv/offbox/envs/.offbox-smoke-owner) == "$fixture_id" ]]
}

cleanup() {
    local failed=0 root name resource owned inventory entries kind owned_workspace=false
    if [[ -e "$task_dir" || -L "$task_dir" ]]; then
        if workspace_owned; then
            owned_workspace=true
        else
            printf '%s\n' 'Refusing cleanup of an unowned or symlinked smoke workspace.' >&2
            failed=1
        fi
    fi
    if "$owned_workspace" && [[ -e "$task_dir/artifacts/manifest.json" ]]; then
        if [[ -L "$task_dir/artifacts/manifest.json" ]] || ! entries=$(jq -er '[.envs[] | [.name, .root] | @tsv] | join("\n")' "$task_dir/artifacts/manifest.json"); then
            failed=1
        else
            while IFS=$'\t' read -r name root; do
                [[ -n "$name" || -n "$root" ]] || continue
                if [[ ! "$name" =~ ^[a-z0-9][a-z0-9-]{0,36}$ || "$root" != "/tmp/offbox-smoke-$fixture_id/worktrees/"* ]]; then
                    failed=1
                    continue
                fi
                if inventory=$(docker ps -aq --filter "label=offbox.worktree=$root"); then
                    while IFS= read -r resource; do
                        [[ -n "$resource" ]] || continue
                        docker logs --tail 100 "$resource" > "$task_dir/artifacts/env-$name.log" 2>&1 || :
                        docker cp "$resource:/var/log/dockerd.log" "$task_dir/artifacts/dockerd-$name.log" 2>/dev/null || :
                        docker rm -f "$resource" >/dev/null || failed=1
                    done <<< "$inventory"
                else
                    failed=1
                fi
                if inventory=$(docker volume ls -q --filter "name=^offbox-$name-docker$"); then
                    if [[ -n "$inventory" ]]; then
                        if owned=$(docker volume inspect --format '{{index .Labels "offbox.worktree"}}' "$inventory") && [[ "$owned" == "$root" ]]; then
                            docker volume rm "$inventory" >/dev/null || failed=1
                        else
                            failed=1
                        fi
                    fi
                else
                    failed=1
                fi
                if kind=$(LC_ALL=C stat --format=%F -- "/srv/offbox/envs/$name" 2>&1); then
                    if [[ "$kind" == directory ]] && srv_owned; then
                        sudo rm -rf -- "/srv/offbox/envs/$name" || failed=1
                    else
                        failed=1
                    fi
                else
                    case "$kind" in *': No such file or directory'|*': Not a directory') ;; *) printf '%s\n' "$kind" >&2; failed=1 ;; esac
                fi
            done <<< "$entries"
        fi
    fi
    if inventory=$(docker ps -aq --filter "label=$label"); then
        while IFS= read -r resource; do
            [[ -n "$resource" ]] || continue
            if "$owned_workspace"; then docker logs --tail 100 "$resource" > "$task_dir/artifacts/container-$resource.log" 2>&1 || :; fi
            docker rm -f "$resource" >/dev/null || failed=1
        done <<< "$inventory"
    else
        failed=1
    fi
    if inventory=$(docker volume ls -q --filter "label=$label"); then
        while IFS= read -r resource; do
            [[ -n "$resource" ]] || continue
            docker volume rm "$resource" >/dev/null || failed=1
        done <<< "$inventory"
    else
        failed=1
    fi
    if inventory=$(docker network ls -q --filter 'name=^offbox$'); then
        if [[ -n "$inventory" ]]; then
            if owned=$(docker network inspect --format '{{index .Labels "offbox.smoke"}}' "$inventory"); then
                if [[ "$owned" == "$fixture_id" ]]; then docker network rm "$inventory" >/dev/null || failed=1; fi
            else
                failed=1
            fi
        fi
    else
        failed=1
    fi
    if "$owned_workspace"; then
        if [[ -e /srv/offbox/envs || -L /srv/offbox/envs ]]; then
            if srv_owned; then
                sudo rm -- /srv/offbox/envs/.offbox-smoke-owner || failed=1
                sudo rmdir -- /srv/offbox/envs || failed=1
            else
                printf '%s\n' 'Refusing cleanup of an unowned or symlinked /srv fixture path.' >&2
                failed=1
            fi
        fi
        if [[ -d "$task_dir/keys" ]]; then sudo rm -rf -- "$task_dir/keys" || failed=1; fi
    fi
    return "$failed"
}

if [[ "$mode" == cleanup ]]; then
    cleanup
    exit
fi
[[ "$mode" == run ]] || exit 2
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { printf '%s\n' 'Smoke tests require native Linux amd64.' >&2; exit 3; }
base_image=${3:?Expected the loaded current-checkout universal image}
fixture_image=${base_image%:*}-fixture:${base_image##*:}
[[ ! -e "$task_dir" && ! -L "$task_dir" ]] || { printf '%s\n' 'Smoke workspace already exists.' >&2; exit 3; }
[[ ! -L /srv && ! -L /srv/offbox && ! -e /srv/offbox/envs && ! -L /srv/offbox/envs ]] || { printf '%s\n' 'Refusing an existing /srv/offbox/envs path.' >&2; exit 3; }
if docker network inspect offbox >/dev/null 2>&1; then
    printf '%s\n' 'Refusing an existing offbox Docker network.' >&2
    exit 3
fi
mkdir -m 0700 -- "$task_dir"
printf '%s\n' "$fixture_id" > "$task_dir/.offbox-smoke-owner"
mkdir -- "$task_dir/artifacts" "$task_dir/keys"
finish() {
    local exit_status=$?
    trap - EXIT
    if ! cleanup; then exit_status=1; fi
    exit "$exit_status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
sudo mkdir -p /srv/offbox/envs
printf '%s\n' "$fixture_id" | sudo tee /srv/offbox/envs/.offbox-smoke-owner >/dev/null
sudo chown 1000:1000 /srv/offbox/envs
sudo chown "1000:$(id -g)" "$task_dir/artifacts"
sudo chmod 0770 "$task_dir/artifacts"
ssh-keygen -q -t ed25519 -N '' -f "$task_dir/keys/client"
ssh-keygen -q -t ed25519 -N '' -f "$task_dir/keys/host"
sudo chown -R 1000:1000 "$task_dir/keys"
docker network create --label "$label" offbox >/dev/null
docker build --builder default --build-arg "UNIVERSAL_IMAGE=$base_image" -t "$fixture_image" -f tests/smoke/Dockerfile .
host=offbox-smoke-host-$fixture_id
docker run -d --name "$host" --label "$label" --network offbox \
    --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
    --mount type=bind,source=/srv/offbox/envs,target=/srv/offbox/envs \
    --mount "type=bind,source=$task_dir/keys,target=/keys,readonly" \
    --entrypoint /bin/sh "$fixture_image" /fixture/host.sh >/dev/null
docker run --name "offbox-smoke-controller-$fixture_id" --label "$label" --network offbox --init \
    --user 1000:1000 --group-add "$(stat -c %g /var/run/docker.sock)" \
    --env "SMOKE_ID=$fixture_id" --env "SMOKE_HOST=$host" --env "SMOKE_BASE_IMAGE=$base_image" --env "SMOKE_FIXTURE_IMAGE=$fixture_image" \
    --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
    --mount type=bind,source=/srv/offbox/envs,target=/srv/offbox/envs \
    --mount "type=bind,source=$PWD,target=/checkout,readonly" \
    --mount "type=bind,source=$task_dir/keys,target=/keys,readonly" \
    --mount "type=bind,source=$task_dir/artifacts,target=/artifacts" \
    --entrypoint /bin/sh "$fixture_image" /checkout/tests/smoke/controller.sh
