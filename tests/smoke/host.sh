#!/bin/sh
set -eu

socket_gid=$(stat -c %g /var/run/docker.sock)
socket_group=$(getent group "$socket_gid" | cut -d: -f1 || :)
if [ -z "$socket_group" ]; then
    groupadd --gid "$socket_gid" fixture-docker
    socket_group=fixture-docker
fi
usermod -aG "$socket_group" dev
passwd -d dev
install -d -m 0700 -o dev -g dev /home/dev/.ssh
install -m 0600 -o dev -g dev /keys/client.pub /home/dev/.ssh/authorized_keys
install -d -m 0755 /run/sshd /var/lib/offbox
install -m 0600 /keys/host /etc/ssh/ssh_host_ed25519_key
install -m 0644 /keys/host.pub /etc/ssh/ssh_host_ed25519_key.pub
touch /var/lib/offbox/ready /var/lib/offbox/lock
chown dev:dev /var/lib/offbox/lock
chmod 0600 /var/lib/offbox/lock
cat > /etc/ssh/sshd_config <<'CONFIG'
Port 22
ListenAddress 0.0.0.0
HostKey /etc/ssh/ssh_host_ed25519_key
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PermitRootLogin no
PubkeyAuthentication yes
AllowUsers dev
AuthorizedKeysFile .ssh/authorized_keys
UsePAM no
AllowTcpForwarding yes
SetEnv BUILDX_BUILDER=default
Subsystem sftp internal-sftp
LogLevel VERBOSE
CONFIG
exec /usr/sbin/sshd -D -e
