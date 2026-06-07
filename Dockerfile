# Dedicated renfield-mcp-samsung image — a small Samsung Tizen TV control server.
#
# Mirrors renfield-mcp-dlna: the Renfield backend never imports this package (it
# talks to it over streamable-http), so a standalone image lets pip install the
# real vendor deps (samsungtvws[encrypted] → cryptography + py3rijndael + WoL)
# instead of bloating the backend image, and decouples release cadence.
#
# Runs on hostNetwork (SSDP discovery + Wake-on-LAN broadcast + websocket/UPnP to
# the TV on the LAN) — see k8s/samsung-mcp.yaml in the renfield repo.
FROM python:3.11-slim

WORKDIR /app

# All runtime deps ship as pure-python or manylinux wheels (samsungtvws,
# cryptography, py3rijndael, websocket-client, requests, wakeonlan) — no compiler
# toolchain needed on slim.
RUN pip install --no-cache-dir --upgrade "pip>=25.3"

COPY pyproject.toml README.md main.py tv.py ./
RUN pip install --no-cache-dir .

# Pairing token persists here (a secret). k8s mounts a PVC at /state so the
# one-time "Allow Renfield?" authorization survives pod restarts. WoL power-on
# and DLNA media/volume do NOT need the token; websocket keys/apps do.
ENV RENFIELD_STATE_DIR=/state \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=9092

RUN mkdir -p /state

EXPOSE 9092

ENTRYPOINT ["renfield-mcp-samsung"]
