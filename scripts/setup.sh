#!/usr/bin/env bash
# One-shot setup for a fresh Raspberry Pi 5 (Pi OS Bookworm).
set -euo pipefail

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip bluetooth bluez

echo "==> Creating venv"
python3 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate

echo "==> Installing Python deps"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Done."
echo
echo "Next steps:"
echo "  1. cp .env.example .env  &&  edit .env"
echo "  2. .venv/bin/python -m src.cli init-db"
echo "  3. .venv/bin/python -m src.cli test-alert    # check Telegram"
echo "  4. .venv/bin/python -m src.cli poll          # check Garmin"
echo "  5. .venv/bin/python -m src.cli ble           # check watch broadcast"
