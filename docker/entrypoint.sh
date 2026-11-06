#!/bin/sh
# Einstiegspunkt des Gateway-Containers.
#   CAN_SETUP=1    SocketCAN-Schnittstelle im Container einrichten (braucht cap_add: NET_ADMIN
#                  und network_mode: host, sonst wirkt es auf einen anderen Netzwerk-Namespace)
#   CAN_IFACE      Schnittstelle (Default can0)
#   CAN_BITRATE    Bitrate in bit/s (Default 50000)
set -eu

DATA_DIR="${GATEWAY_DATA_DIR:-/data}"
CONFIG="${DATA_DIR}/gateway.yaml"

mkdir -p "${DATA_DIR}" "${DATA_DIR}/logs"

if [ "${CAN_SETUP:-0}" = "1" ]; then
    IFACE="${CAN_IFACE:-can0}"
    BITRATE="${CAN_BITRATE:-50000}"
    echo "entrypoint: richte ${IFACE} mit ${BITRATE} bit/s ein"
    ip link set "${IFACE}" down || true
    if ip link set "${IFACE}" type can bitrate "${BITRATE}" && ip link set "${IFACE}" up; then
        echo "entrypoint: ${IFACE} ist aktiv"
    else
        echo "entrypoint: WARNUNG: ${IFACE} konnte nicht eingerichtet werden (NET_ADMIN? Adapter vorhanden?)" >&2
    fi
fi

if [ ! -f "${CONFIG}" ]; then
    echo "entrypoint: erzeuge Standard-Konfiguration ${CONFIG}"
    cat > "${CONFIG}" <<YAML
# Erzeugt vom Container-Entrypoint. Einstellungen bitte in der Weboberfläche (Port 8080) vornehmen.
version: 1

service:
  data_dir: auto
  log_level: INFO

can:
  enabled: false          # in der Weboberfläche aktivieren, sobald can0 vorhanden ist
  interface: socketcan
  channel: can0
  bitrate: ${CAN_BITRATE:-50000}

modbus:
  bind: 0.0.0.0
  port: 502

web:
  bind: 0.0.0.0           # Passwort beim ersten Aufruf der Weboberfläche setzen!
  port: 8080

devices: []
YAML
fi

exec gateway "$@"
