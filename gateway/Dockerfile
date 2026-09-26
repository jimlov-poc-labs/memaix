FROM python:3.12-slim

WORKDIR /app

# git krävs för minnes-vaults (commit per skrivning); fonts-dejavu-core ger
# email_export_pdf ett typsnitt med €, tankstreck m.m. (tools/mail_pdf.py)
RUN apt-get update && apt-get install -y --no-install-recommends git fonts-dejavu-core \
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
