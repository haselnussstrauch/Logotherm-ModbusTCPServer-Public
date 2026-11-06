# syntax=docker/dockerfile:1
# Logotherm Modbus-Gateway – Container-Image (amd64 + arm64)
# Build (aus dem Repo-Root):  docker build -f docker/Dockerfile -t logotherm-gateway .
# Multi-Arch:                 docker buildx build --platform linux/amd64,linux/arm64 -f docker/Dockerfile .

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- Stufe 1: Wheels bauen (Abhängigkeiten laut requirements.lock + Gateway-Paket) ----------
FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.lock ./
# Compiler nur hier (z. B. falls für arm64 ein Paket ohne fertiges Wheel gebaut werden muss).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
# Test-Werkzeuge (pytest*) aus dem Lock gehören nicht ins Image.
RUN mkdir /wheels \
 && grep -viE '^(pytest|pluggy|iniconfig)' requirements.lock > /wheels/runtime-requirements.txt \
 && pip wheel --wheel-dir /wheels -r /wheels/runtime-requirements.txt
COPY pyproject.toml README.md ./
COPY gateway ./gateway
RUN pip wheel --wheel-dir /wheels --no-deps .

# ---- Stufe 2: Runtime ----------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    GATEWAY_DATA_DIR=/data

# iproute2: optionales "ip link set can0 ..." (CAN_SETUP=1, braucht NET_ADMIN).
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 \
 && rm -rf /var/lib/apt/lists/*

# Exakt die gepinnten Versionen installieren (--no-index: nur aus den gebauten Wheels).
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-index --find-links=/wheels --no-deps -r /wheels/runtime-requirements.txt \
 && pip install --no-index --find-links=/wheels --no-deps logotherm-gateway

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
 && mkdir -p /data && chmod 0777 /data

# Konfiguration, Logs, Zustand. Beim Betrieb als anderer User muss das Volume diesem gehören.
VOLUME /data
WORKDIR /data

# Weboberfläche und Modbus-TCP (bei network_mode: host nur Dokumentation).
EXPOSE 8080 502

# Port wird aus /data/gateway.yaml (web.port) gelesen, Default 8080. Kein curl nötig.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/usr/local/bin/healthcheck.py"]

# Standard: Root im Container. Das ist bei SocketCAN + network_mode: host üblich
# (Port 502 und ggf. "ip link" brauchen Rechte). Varianten siehe docs/docker.md:
#   - Nicht-Root:  user: "1000:1000" + Host-sysctl net.ipv4.ip_unprivileged_port_start=0
#                  oder modbus.port auf >1024 stellen.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["run"]
