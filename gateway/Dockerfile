FROM python:3.12-slim

WORKDIR /app

# git krävs för minnes-vaults (commit per skrivning)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r -g 1000 memaix \
    && useradd -r -u 1000 -g memaix -d /app -s /sbin/nologin memaix \
    && mkdir -p /data && chown memaix:memaix /data

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir . \
    && chown -R memaix:memaix /app

EXPOSE 8080
ENV MEMAIX_TRANSPORT=http
ENV MEMAIX_DATA_DIR=/data
USER memaix
CMD ["python", "-m", "memaix_gateway.server"]
