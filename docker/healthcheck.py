"""Docker-HEALTHCHECK: fragt /api/status ab; Exit 0 = gesund, 1 = ungesund."""

import json
import os
import sys
import urllib.request


def _port() -> int:
    path = os.path.join(os.environ.get("GATEWAY_DATA_DIR", "/data"), "gateway.yaml")
    try:
        import yaml

        with open(path, encoding="utf-8") as f:
            return int(((yaml.safe_load(f) or {}).get("web") or {}).get("port", 8080))
    except Exception:  # noqa: BLE001 - Datei fehlt/kaputt -> Default
        return 8080


def main() -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_port()}/api/status", timeout=4) as r:
            return 0 if r.status == 200 and json.load(r).get("running") else 1
    except Exception:  # noqa: BLE001
        return 1


if __name__ == "__main__":
    sys.exit(main())
