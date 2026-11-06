"""PyInstaller-Einstieg für gateway.exe (entspricht dem Konsolenbefehl `gateway`)."""

import sys

from gateway.main import cli

if __name__ == "__main__":
    sys.exit(cli())
