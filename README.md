# AIUsageTray

A cross-platform PyQt6 system tray monitor for AI usage limits across **Antigravity**, **OpenAI Codex**, **Claude**, **NotebookLM**, **Gemini Web**, and **GitHub Copilot**.

## Providers & Features
- **System Tray & Panel**: Dynamic ring-gauge tray icon showing the lowest remaining quota across providers, left-click floating panel with overview, provider tabs, and analytics, plus a right-click context menu.
- **Antigravity**:
  - **Local Language Server (No-Auth)**: Automatically discovers the running `language_server` process and listening port (`ps`/`ss` on Linux, `ps`/`lsof` on macOS, PowerShell `Get-CimInstance`/`netstat` on Windows) and queries `/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary`.
  - **OAuth Fallback**: Google OAuth 2.0 PKCE against `cloudcode-pa.googleapis.com`.
  - **Full 7-Decimal Precision**: Exact `Decimal` percentage formatting (e.g. `99.68221%`).
- **OpenAI (Codex)**:
  - **Codex Binary (`app-server`)**: Queries `account/rateLimits/read` via JSON-RPC from `codex` on `PATH` or installed desktop app paths across Linux, macOS, and Windows.
  - **OAuth**: Auto-discovers `~/.codex/auth.json` or runs interactive OAuth PKCE against `https://chatgpt.com/backend-api/wham/usage`.
- **Claude**:
  - **OAuth**: Supports interactive OAuth PKCE, Claude Code CLI (`~/.claude/.credentials.json` / keyring), and Claude Desktop `oauth:tokenCacheV2` against `https://api.anthropic.com/api/oauth/usage` with `429` backoff.
  - **Claude Desktop & Browser Cookies**: Decrypts `sessionKey` / `lastActiveOrg` from Claude Desktop or Chromium-based browsers.
- **NotebookLM**:
  - **Browser Cookies**: Uses Google session cookies (`SID` / `__Secure-1PSID`) from installed Chromium-based browsers to query `notebook.google.com` (`EylDcb` batchexecute RPC) for 5-hour/weekly limits and plan tier.
- **Gemini Web**:
  - **Browser Cookies**: Uses Google session cookies from installed Chromium-based browsers to query `gemini.google.com` (`GEMINI_WEB_GRAPHQL` / `batchexecute`) for 5-hour and weekly quota pools.
- **GitHub Copilot**:
  - **Local Token & Device Flow**: Discovers OAuth tokens from local GitHub Copilot config (`hosts.json` / `apps.json`), `gh` CLI (`hosts.yml` / `gh auth token`), or interactive GitHub OAuth device flow against `https://api.github.com/copilot_internal/user`.
- **Extensible Architecture**: Modular `UsageProvider` registry and SQLite history store (`history.db`) with schema ready for future token tracking.

## Platform Support

| Platform | Status | Config / Data Paths | Cookie / SafeStorage Decryption |
| --- | --- | --- | --- |
| **Linux** (KDE Plasma / XDG) | Native | `~/.config/ai-usage-tray`, `~/.local/share/ai-usage-tray` | SecretService / KDE Wallet (`secretstorage`) |
| **macOS** | Native | `~/Library/Application Support/ai-usage-tray` | macOS Keychain (`security` CLI, 1003-iter PBKDF2) |
| **Windows** | Native | `%APPDATA%\ai-usage-tray`, `%LOCALAPPDATA%\ai-usage-tray` | DPAPI (`CryptUnprotectData`) + `Local State` AES-256-GCM |

> **Note (Windows Chrome `v20`):** Chrome 127+ on Windows uses App-Bound Encryption (`v20` cookies), which requires an elevated system service to decrypt. Unelevated user runs still decrypt `v10`/`v11` Electron/Chromium storage (e.g., Claude Desktop, Edge/Brave/older Chrome), and OAuth / CLI / local server providers work normally.

## Quickstart

Requires **Python 3.11+**.

### Linux (KDE Plasma / systemd)
Install as a `systemd --user` service using system Python packages (avoids `pip` / `--break-system-packages`):
```bash
./install.sh
```
Or run directly from the repo:
```bash
python3 -m ai_usage_tray
```

### macOS
Install into an isolated environment with `pipx` or a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
ai-usage-tray
```
Or with `pipx`:
```bash
pipx install .
ai-usage-tray
```

### Windows (PowerShell)
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install .
ai-usage-tray
```
Or with `pipx`:
```powershell
pipx install .
ai-usage-tray
```

