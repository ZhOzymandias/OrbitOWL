#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd -- "$root"
[[ "$EUID" -ne 0 ]] || { echo "Esegui lo script come proprietario del clone, senza sudo."; exit 1; }
[[ "$(git branch --show-current)" == main ]] || { echo "Il clone deve essere su main."; exit 1; }
[[ "$root" =~ ^/[a-zA-Z0-9._/-]+$ ]] || { echo "Usa un percorso senza spazi."; exit 1; }
test -r .env || { echo "Prima esegui python3 scripts/setup.py."; exit 1; }
git config --local pull.ff only

sudo install -d -m 0755 /usr/local/lib/orbitowl
sudo install -m 0644 scripts/apply_update.py /usr/local/lib/orbitowl/apply_update.py
sudo tee /etc/systemd/system/orbitowl-update.service >/dev/null <<UNIT
[Unit]
Description=Apply local OrbitOWL main checkout
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
TimeoutStartSec=infinity
ExecStart=/usr/bin/python3 /usr/local/lib/orbitowl/apply_update.py $root
UNIT
sudo tee /etc/systemd/system/orbitowl-update.timer >/dev/null <<'UNIT'
[Unit]
Description=Check for pulled OrbitOWL code

[Timer]
OnBootSec=1min
OnUnitInactiveSec=1min
Unit=orbitowl-update.service

[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now orbitowl-update.timer
sudo systemctl start orbitowl-update.service
