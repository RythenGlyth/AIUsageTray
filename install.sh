#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${HOME}/.local/share/ai-usage-tray/app"
BIN_DIR="${HOME}/.local/bin"
LAUNCHER_PATH="${BIN_DIR}/ai-usage-tray"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_PATH="${UNIT_DIR}/ai-usage-tray.service"

python3 - <<'PY' >/dev/null 2>&1 || {
import PyQt6  # noqa: F401
import keyring  # noqa: F401
import requests  # noqa: F401
import cryptography  # noqa: F401
import secretstorage  # noqa: F401
PY
    echo "[install] Missing required Python packages (PyQt6, keyring, requests, cryptography, secretstorage)." >&2
    exit 1
}

mkdir -p "${APP_DIR}" "${BIN_DIR}" "${UNIT_DIR}"
rm -rf "${APP_DIR}/ai_usage_tray"
cp -r "${SCRIPT_DIR}/ai_usage_tray" "${APP_DIR}/ai_usage_tray"

cat > "${LAUNCHER_PATH}" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="${APP_DIR}\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m ai_usage_tray "\$@"
EOF
chmod +x "${LAUNCHER_PATH}"

sed "s|@LAUNCHER_PATH@|${LAUNCHER_PATH}|g" \
    "${SCRIPT_DIR}/packaging/systemd/ai-usage-tray.service" > "${UNIT_PATH}"

systemctl --user daemon-reload
systemctl --user enable --now ai-usage-tray.service

echo "[install] AIUsageTray installed and started via systemd --user (ai-usage-tray.service)."
