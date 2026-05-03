#!/usr/bin/env bash
# Container entrypoint for the traider MCP hub.
#
# Always serves the HTTP transport over TLS — Claude Desktop's
# remote-MCP integration only connects to https:// URLs, so the
# container default matches that requirement. On first start the
# script mints a self-signed cert into ${TRAIDER_CERT_DIR:-/certs}
# (mounted from the host's ./certs by docker-compose) and reuses it
# on every subsequent start; deleting the files forces a fresh one.
#
# Subcommands that don't run the HTTP transport (e.g. `auth schwab`)
# are passed through unmodified.
set -euo pipefail

CERT_DIR="${TRAIDER_CERT_DIR:-/certs}"
CERT_FILE="${CERT_DIR}/traider.pem"
KEY_FILE="${CERT_DIR}/traider-key.pem"

mkdir -p "${CERT_DIR}"

if [[ ! -s "${CERT_FILE}" || ! -s "${KEY_FILE}" ]]; then
    echo "traider-entrypoint: generating self-signed TLS cert in ${CERT_DIR}" >&2
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -days 825 \
        -subj "/CN=traider" \
        -addext "subjectAltName=DNS:localhost,DNS:traider,IP:127.0.0.1,IP:0:0:0:0:0:0:0:1" \
        >/dev/null 2>&1
    chmod 600 "${KEY_FILE}"
    echo "traider-entrypoint: cert generated; clients connecting from outside" \
         "the container will need to trust ${CERT_FILE} (or replace it with" \
         "an mkcert-issued pair)" >&2
fi

# `auth schwab` and any other non-server subcommand: pass through as-is.
if [[ "${1:-}" == "auth" ]]; then
    exec conda run --no-capture-output -n traider traider "$@"
fi

# Server mode: prepend the TLS flags. They sit before "$@" so any
# operator-supplied --ssl-certfile / --ssl-keyfile on the docker run
# command line still wins (argparse last-value wins on store actions).
exec conda run --no-capture-output -n traider traider \
    --ssl-certfile "${CERT_FILE}" \
    --ssl-keyfile "${KEY_FILE}" \
    "$@"
