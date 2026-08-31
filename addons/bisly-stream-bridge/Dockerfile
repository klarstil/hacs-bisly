ARG MEDIAMTX_VERSION=v1.11.3

FROM python:3.12-slim AS build

ARG TARGETARCH
ARG MEDIAMTX_VERSION

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
        arm64) ARCH=arm64v8 ;; \
        armhf) ARCH=armv6 ;; \
        *) ARCH="${TARGETARCH}" ;; \
    esac; \
    curl -Lsf -o /tmp/mediamtx.tar.gz \
        "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_${ARCH}.tar.gz" \
    && mkdir -p /opt/mediamtx \
    && tar -xzf /tmp/mediamtx.tar.gz -C /opt/mediamtx \
    && rm /tmp/mediamtx.tar.gz

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg jq \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/mediamtx /opt/mediamtx

WORKDIR /opt/bisly-bridge

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY bridge/ ./bridge/
COPY run.sh /opt/run.sh
COPY start_camera.sh /opt/start_camera.sh
COPY stop_camera.sh /opt/stop_camera.sh
COPY mediamtx.yml /opt/mediamtx.yml

RUN chmod +x /opt/run.sh /opt/start_camera.sh /opt/stop_camera.sh /opt/mediamtx/mediamtx

ENTRYPOINT ["/opt/run.sh"]
