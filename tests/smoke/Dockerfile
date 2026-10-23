ARG UNIVERSAL_IMAGE
FROM ${UNIVERSAL_IMAGE}

USER root
ARG MUTAGEN_VERSION=0.18.1
ARG MUTAGEN_SHA256=7735286c778cc438418209f24d03a64f3a0151c8065ef0fe079cfaf093af6f8f
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server \
    && curl -fsSL --retry 3 --max-time 120 "https://github.com/mutagen-io/mutagen/releases/download/v${MUTAGEN_VERSION}/mutagen_linux_amd64_v${MUTAGEN_VERSION}.tar.gz" -o /tmp/mutagen.tar.gz \
    && printf '%s  /tmp/mutagen.tar.gz\n' "$MUTAGEN_SHA256" | sha256sum -c - \
    && tar -xzf /tmp/mutagen.tar.gz -C /usr/local/bin mutagen mutagen-agents.tar.gz \
    && chmod 0755 /usr/local/bin/mutagen \
    && rm -f /tmp/mutagen.tar.gz \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=0755 tests/smoke/host.sh /fixture/host.sh
