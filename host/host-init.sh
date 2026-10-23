#!/bin/sh
set -eu

fail() {
    printf 'offbox: %s\n  Fix: %s\n' "$1" "$2" >&2
    exit 3
}

[ "$(id -u)" -eq 0 ] || fail 'Bootstrap requires root.' 'Run offbox host init with passwordless sudo access.'
[ "$#" -eq 2 ] || fail 'Expected image and SSH public key arguments.' 'Invoke bootstrap through offbox host create or host init.'
image=$1
public_key=$2
harden=${OFFBOX_HARDEN:-1}
version=${OFFBOX_VERSION:-}
case "$harden" in 0|1) ;; *) fail 'Invalid OFFBOX_HARDEN.' 'Use 1 for cloud hosts or 0 for an existing host.' ;; esac
case "$version" in ''|*[!A-Za-z0-9.+_-]*) fail 'Missing or invalid OFFBOX_VERSION.' 'Pass the installed offbox version through OFFBOX_VERSION.' ;; esac
case "$image" in ''|-*) fail 'Invalid container image.' 'Pass the configured offbox image.' ;; esac
case "$public_key" in
    ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*\ *|sk-ssh-ed25519@openssh.com\ *|sk-ecdsa-sha2-*\ *) ;;
    *) fail 'Invalid SSH public key.' 'Select an OpenSSH public key file.' ;;
esac
newline='
'
carriage_return=$(printf '\r')
case "$public_key" in *"$newline"*|*"$carriage_return"*) fail 'SSH public key must contain one line.' 'Select a single OpenSSH public key.' ;; esac
[ -f /etc/os-release ] || fail 'Cannot identify host OS.' 'Use Ubuntu 24.04.'
os_id=$(awk -F= '$1 == "ID" {gsub(/"/, "", $2); print $2}' /etc/os-release)
os_version=$(awk -F= '$1 == "VERSION_ID" {gsub(/"/, "", $2); print $2}' /etc/os-release)
[ "$os_id:$os_version" = ubuntu:24.04 ] || fail 'Host must run Ubuntu 24.04.' 'Use an Ubuntu 24.04 host.'

uid_owner=$(getent passwd 1000 | cut -d: -f1 || true)
if [ -n "$uid_owner" ] && [ "$uid_owner" != offbox ]; then
    if [ "$harden" != 1 ] || [ "$uid_owner" != ubuntu ]; then
        fail "UID 1000 belongs to $uid_owner; refusing to change that account." 'Use a dedicated host with UID 1000 available, or move the account manually before retrying.'
    fi
    if pgrep -u ubuntu >/dev/null; then
        fail 'The default ubuntu account has running processes.' 'Use a fresh cloud host without an active ubuntu session.'
    fi
fi
if id offbox >/dev/null 2>&1; then
    [ "$(getent passwd offbox | cut -d: -f6)" = /home/offbox ] || fail 'Existing offbox account has an unexpected home directory.' 'Resolve the account configuration before retrying.'
    [ "$(id -u offbox)" -eq 1000 ] || fail 'Existing offbox account does not have UID 1000.' 'Use a dedicated host or resolve the conflicting offbox account manually.'
fi
for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
        fail "Conflicting package $package is installed." 'Review the existing Docker installation and remove conflicting packages before retrying.'
    fi
done
installed_docker=$(dpkg-query -W -f='${Version}' docker-ce 2>/dev/null || true)
case "$installed_docker" in ''|5:29.*) ;; *) fail "Existing Docker version $installed_docker differs from required major 29." 'Review and migrate Docker to major 29 before retrying.' ;; esac

work_dir=$(mktemp -d)
cleanup() {
    result=$?
    rm -rf "$work_dir"
    if [ "$result" -ne 0 ] && [ "$result" -ne 3 ]; then
        printf 'offbox: bootstrap failed.\n  Fix: Review the bootstrap log and retry after resolving the failure.\n' >&2
    fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

install_config() {
    source_file=$1
    target_file=$2
    mode=$3
    if [ ! -f "$target_file" ] || ! cmp -s "$source_file" "$target_file"; then
        install -m "$mode" "$source_file" "$target_file"
        config_changed=1
    fi
    chmod "$mode" "$target_file"
}

printf '%s\n' "$public_key" > "$work_dir/public_key"
ssh-keygen -lf "$work_dir/public_key" >/dev/null 2>&1 || fail 'SSH public key cannot be parsed.' 'Select a valid OpenSSH public key.'

install -d -m 0755 /var/lib/offbox
rm -f /var/lib/offbox/ready
if [ "$uid_owner" = ubuntu ]; then
    userdel -r ubuntu
fi
if ! getent group offbox >/dev/null; then
    groupadd offbox
fi
if ! id offbox >/dev/null 2>&1; then
    useradd --uid 1000 --gid offbox --create-home --shell /bin/bash --password '*' offbox
fi
usermod --lock offbox
install -d -m 0700 -o offbox -g offbox /home/offbox/.ssh
printf '%s\n' "$public_key" > "$work_dir/authorized_keys"
install_config "$work_dir/authorized_keys" /home/offbox/.ssh/authorized_keys 0600
chown offbox:offbox /home/offbox/.ssh/authorized_keys

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl sudo jq
if [ "$harden" = 1 ]; then
    apt-get install -y ufw fail2ban unattended-upgrades
fi
printf 'offbox ALL=(ALL) NOPASSWD:ALL\n' > "$work_dir/sudoers"
visudo -cf "$work_dir/sudoers"
install_config "$work_dir/sudoers" /etc/sudoers.d/offbox 0440

if [ "$harden" = 1 ]; then
    install -d -m 0755 /etc/ssh/sshd_config.d
    cat > "$work_dir/sshd" <<'SSH'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers offbox
SSH
    config_changed=0
    install_config "$work_dir/sshd" /etc/ssh/sshd_config.d/00-offbox.conf 0644
    /usr/sbin/sshd -t
    /usr/sbin/sshd -T > "$work_dir/sshd-effective"
    for setting in 'passwordauthentication no' 'kbdinteractiveauthentication no' 'permitrootlogin no' 'allowusers offbox'; do
        grep -qx "$setting" "$work_dir/sshd-effective" || fail 'SSH hardening was overridden by host configuration.' 'Review sshd_config before retrying bootstrap.'
    done
    if [ "$config_changed" = 1 ]; then
        systemctl reload ssh
    fi
    ufw default deny incoming
    ufw allow 22/tcp
    ufw --force enable
    printf '[sshd]\nenabled = true\nbackend = systemd\n' > "$work_dir/fail2ban"
    config_changed=0
    install_config "$work_dir/fail2ban" /etc/fail2ban/jail.d/offbox.local 0644
    systemctl enable --now fail2ban
    if [ "$config_changed" = 1 ]; then
        systemctl restart fail2ban
    fi
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > "$work_dir/auto-upgrades"
    install_config "$work_dir/auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades 0644
    systemctl enable --now unattended-upgrades
fi

install -d -m 0755 /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL --max-time 60 https://download.docker.com/linux/ubuntu/gpg -o "$work_dir/docker.asc"
    install_config "$work_dir/docker.asc" /etc/apt/keyrings/docker.asc 0644
fi
architecture=$(dpkg --print-architecture)
cat > "$work_dir/docker.sources" <<APT
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $architecture
Signed-By: /etc/apt/keyrings/docker.asc
APT
install_config "$work_dir/docker.sources" /etc/apt/sources.list.d/docker.sources 0644
cat > "$work_dir/docker.preferences" <<'APT'
Package: docker-ce docker-ce-cli
Pin: version 5:29.*
Pin-Priority: 1001
APT
install_config "$work_dir/docker.preferences" /etc/apt/preferences.d/offbox-docker 0644
apt-get update
if [ -n "$installed_docker" ]; then
    docker_version=$installed_docker
else
    docker_version=$(apt-cache madison docker-ce | awk '$3 ~ /^5:29[.]/ {print $3; exit}')
fi
[ -n "$docker_version" ] || fail 'Docker major 29 is unavailable in the official repository.' 'Check Docker repository access and package availability.'
apt-get install -y "docker-ce=$docker_version" "docker-ce-cli=$docker_version" containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker offbox
systemctl enable --now docker
if docker network inspect offbox >/dev/null 2>&1; then
    subnet=$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' offbox)
    [ "$subnet" = 10.89.0.0/16 ] || fail 'The existing offbox network has a different subnet.' 'Remove the conflicting offbox network after reviewing its attached containers.'
else
    docker network create --subnet 10.89.0.0/16 offbox
fi
install -d -m 0750 -o offbox -g offbox /srv/offbox/envs
if [ ! -e /var/lib/offbox/lock ]; then
    touch /var/lib/offbox/lock
fi
chown offbox:offbox /var/lib/offbox/lock
chmod 0600 /var/lib/offbox/lock
if ! docker pull -- "$image"; then
    printf 'offbox: warning: image pull failed; offbox up will retry.\n' >&2
fi
printf '%s\n' "$version" > /var/lib/offbox/version
touch /var/lib/offbox/ready
