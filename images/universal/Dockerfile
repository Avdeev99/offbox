# syntax=docker/dockerfile:1
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG TARGETARCH
ARG NODE_VERSION=22.23.4
ARG NODE_SHA256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
ARG GO_VERSION=1.27.3
ARG GO_SHA256=65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40
ARG UV_VERSION=0.13.5
ARG UV_SHA256=f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f
ARG BUN_VERSION=1.4.4
ARG BUN_SHA256=e0446eb55995772eb9894bebae9a6be368d985a8b9271ee124486f9946eedd76
ARG MISE_VERSION=2026.10.18
ARG MISE_SHA256=e97c38973336aa2ff350fea50f47b412b266fe263b102b7200ff1e9b118c6942
ARG RUSTUP_VERSION=1.29.2
ARG RUSTUP_SHA256=5be191e8c70a5007074254cb74d78dbfdd25cd5a61c27de7b8e5410122ec24f4
ARG RUST_VERSION=1.99.1
ARG PLAYWRIGHT_VERSION=1.64.1
ARG DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    HOME=/home/dev \
    CARGO_HOME=/home/dev/.cargo \
    RUSTUP_HOME=/home/dev/.rustup \
    GOPATH=/home/dev/go \
    PLAYWRIGHT_BROWSERS_PATH=/home/dev/.cache/ms-playwright \
    PATH=/home/dev/.local/share/mise/shims:/home/dev/.local/bin:/home/dev/.cargo/bin:/home/dev/go/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN test "$TARGETARCH" = amd64 \
    && if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi \
    && groupadd --gid 1000 dev \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash dev \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential curl wget jq unzip xz-utils make cmake pkg-config ca-certificates git openssh-client \
        sudo tini locales pipx procps util-linux iptables iproute2 kmod gpg \
        postgresql-client redis-tools sqlite3 ffmpeg libpq-dev libssl-dev \
    && locale-gen en_US.UTF-8 \
    && printf 'dev ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL --max-time 60 https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.asc \
    && docker_key_fingerprints=$(GNUPGHOME=$(mktemp -d) gpg --batch --show-keys --with-colons /tmp/docker.asc | awk -F: '$1 == "pub" {primary = 1; next} primary && $1 == "fpr" {print $10; primary = 0}') \
    && test "$docker_key_fingerprints" = "$DOCKER_KEY_FINGERPRINT" \
    && install -m 0644 /tmp/docker.asc /etc/apt/keyrings/docker.asc \
    && rm /tmp/docker.asc \
    && printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n' > /etc/apt/sources.list.d/docker.sources \
    && printf 'Package: docker-ce docker-ce-cli\nPin: version 5:29.*\nPin-Priority: 1001\n' > /etc/apt/preferences.d/offbox-docker \
    && apt-get update \
    && docker_version=$(apt-cache madison docker-ce | awk '$3 ~ /^5:29[.]/ {print $3; exit}') \
    && test -n "$docker_version" \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        "docker-ce=$docker_version" "docker-ce-cli=$docker_version" containerd.io docker-buildx-plugin docker-compose-plugin \
    && usermod -aG docker dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL --retry 3 --max-time 120 "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz \
    && printf '%s  /tmp/node.tar.xz\n' "$NODE_SHA256" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && corepack enable \
    && curl -fsSL --retry 3 --max-time 120 "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tar.gz \
    && printf '%s  /tmp/go.tar.gz\n' "$GO_SHA256" | sha256sum -c - \
    && tar -xzf /tmp/go.tar.gz -C /usr/local \
    && curl -fsSL --retry 3 --max-time 120 "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" -o /tmp/uv.tar.gz \
    && printf '%s  /tmp/uv.tar.gz\n' "$UV_SHA256" | sha256sum -c - \
    && tar -xzf /tmp/uv.tar.gz -C /tmp \
    && install -m 0755 /tmp/uv-x86_64-unknown-linux-gnu/uv /tmp/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/ \
    && curl -fsSL --retry 3 --max-time 120 "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64-baseline.zip" -o /tmp/bun.zip \
    && printf '%s  /tmp/bun.zip\n' "$BUN_SHA256" | sha256sum -c - \
    && unzip -q /tmp/bun.zip -d /tmp \
    && install -m 0755 /tmp/bun-linux-x64-baseline/bun /usr/local/bin/bun \
    && curl -fsSL --retry 3 --max-time 120 "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-x64" -o /tmp/mise \
    && printf '%s  /tmp/mise\n' "$MISE_SHA256" | sha256sum -c - \
    && install -m 0755 /tmp/mise /usr/local/bin/mise \
    && npx -y "playwright@${PLAYWRIGHT_VERSION}" install-deps chromium \
    && rm -rf /tmp/node.tar.xz /tmp/go.tar.gz /tmp/uv.tar.gz /tmp/uv-x86_64-unknown-linux-gnu /tmp/bun.zip /tmp/bun-linux-x64-baseline /tmp/mise /home/dev/.npm /var/lib/apt/lists/* \
    && chown -R dev:dev /home/dev

USER dev
RUN uv python install 3.13 --default \
    && uv tool install poetry \
    && curl -fsSL --retry 3 --max-time 120 "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" -o /tmp/rustup-init \
    && printf '%s  /tmp/rustup-init\n' "$RUSTUP_SHA256" | sha256sum -c - \
    && chmod 0755 /tmp/rustup-init \
    && /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain "$RUST_VERSION" \
    && rm /tmp/rustup-init \
    && uv cache clean \
    && printf '\neval "$(mise activate bash)"\n' >> /home/dev/.bashrc

USER root
COPY --chmod=0755 images/universal/offbox-init images/universal/offbox-run /usr/local/bin/
RUN install -d -m 0755 /run/offbox \
    && install -d -m 0700 -o dev -g dev /run/offbox/exec \
    && install -d -m 0755 -o dev -g dev /work /home/dev/.cache/ms-playwright \
    && chown -R dev:dev /home/dev/.cache \
    && printf 'export PATH="/home/dev/.local/share/mise/shims:/home/dev/.local/bin:/home/dev/.cargo/bin:/home/dev/go/bin:/usr/local/go/bin:$PATH"\n' > /etc/profile.d/offbox-path.sh

WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/offbox-init"]
