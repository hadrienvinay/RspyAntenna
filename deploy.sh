#!/usr/bin/env bash
#
# deploy.sh — Copie les fichiers du repo vers le Raspberry Pi et redémarre
# le service monitor.
#
# Usage :
#   ./deploy.sh              (utilise les valeurs par défaut ci-dessous)
#   PI_HOST=192.168.1.20 PI_USER=suri ./deploy.sh
#
set -euo pipefail

PI_USER="${PI_USER:-suri}"
PI_HOST="${PI_HOST:-192.168.1.20}"
PI_DIR="${PI_DIR:-/home/suri/monitor}"

# Fichiers déployés sur le Pi (le code applicatif — pas les configs
# systemd/gnss-sdr qui se réinstallent séparément, voir README.txt).
FILES=(
  monitor.py
  dashboard.html
  noaa_tracker.py
  space_tracker.py
  server.py
)

DEST="${PI_USER}@${PI_HOST}:${PI_DIR}/"

echo "→ Copie vers ${DEST}"
scp "${FILES[@]}" "${DEST}"

echo "→ Redémarrage du service monitor"
ssh "${PI_USER}@${PI_HOST}" "sudo systemctl restart monitor"

echo "→ Statut du service"
ssh "${PI_USER}@${PI_HOST}" "sudo systemctl status monitor --no-pager -l | head -n 15"

echo
echo "✓ Déployé. Dashboard → http://${PI_HOST}:8888"
