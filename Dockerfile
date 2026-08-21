FROM python:3.14.3-slim

# provided by BuildKit for the target platform (amd64 or arm64)
ARG TARGETARCH
ARG SING_BOX_VERSION=1.13.19
ARG SING_BOX_SHA256=77e26226c111b8a269f559aec7999f6f5ae1961f25374b58b126d06405d4f516
# linux-arm64-glibc artifact for the same release
ARG SING_BOX_SHA256_ARM64=c79c76bf2f804579768ad4683dc58ff7f3873f0e8159131219290f1ae79b2a38
ARG OPENROT_VERSION=0.0.0

ENV POETRY_VERSION=2.3.2
ENV POETRY_DYNAMIC_VERSIONING_BYPASS=${OPENROT_VERSION}
ENV OPENROT_DIR=/root/.config/openrot
ENV OPENROT_LISTEN=0.0.0.0

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) SG_ARCH=amd64; SG_SHA256=${SING_BOX_SHA256};; \
      arm64) SG_ARCH=arm64; SG_SHA256=${SING_BOX_SHA256_ARM64};; \
      *) echo "unsupported arch: ${TARGETARCH}" >&2; exit 1;; \
    esac; \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fL -o /tmp/sing-box.tar.gz \
        https://github.com/SagerNet/sing-box/releases/download/v${SING_BOX_VERSION}/sing-box-${SING_BOX_VERSION}-linux-${SG_ARCH}-glibc.tar.gz && \
    echo "${SG_SHA256}  /tmp/sing-box.tar.gz" | sha256sum -c - && \
    tar -xzf /tmp/sing-box.tar.gz -C /tmp && \
    install -m 0755 /tmp/sing-box-*/sing-box /usr/local/bin/sing-box && \
    rm -rf /var/lib/apt/lists/* /tmp/*

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}" && \
    poetry config virtualenvs.create false && \
    poetry install --only main --no-root --no-interaction

COPY . .
RUN poetry install --only main --no-interaction && rm -rf ~/.cache
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 7890 7891
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["both"]