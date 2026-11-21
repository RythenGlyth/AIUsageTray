#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${HOME}/.local/share/ai-usage-tray/app"
LAUNCHER_PATH="${HOME}/.local/bin/ai-usage-tray"
UNIT_PATH="${HOME}/.config/systemd/user/ai-usage-tray.service"

systemctl --user disable --now ai-usage-tray.service 2>/dev/null || true
rm -f "${UNIT_PATH}" "${LAUNCHER_PATH}"
rm -rf "${APP_DIR}"
systemctl --user daemon-reload || true

echo "[uninstall] AIUsageTray removed."
