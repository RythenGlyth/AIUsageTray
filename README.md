# AIUsageTray

A KDE Plasma system tray monitor for AI usage limits across **Antigravity**, **OpenAI Codex**, and **Claude**.

## Features
- **KDE Plasma System Tray**: Dynamic ring-gauge icon showing the lowest remaining quota across providers, left-click floating panel with provider tabs, and right-click context menu.
- **Antigravity**:
  - **Local Language Server (No-Auth)**: Automatically discovers the running `language_server` process via `ps aux` and `ss -tlpn` and queries `/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary`.
  - **OAuth Fallback**: Google OAuth 2.0 PKCE against `cloudcode-pa.googleapis.com`.
  - **Full 7-Decimal Precision**: Exact `Decimal` percentage formatting (e.g. `99.68221%`).
- **OpenAI (Codex)**:
  - **Codex Binary (`app-server`)**: Queries `account/rateLimits/read` via JSON-RPC from `codex` on `PATH` or installed desktop app paths (e.g. `/usr/lib/chatgpt/resources/codex`).
  - **OAuth**: Auto-discovers `~/.codex/auth.json` or runs interactive OAuth PKCE against `https://chatgpt.com/backend-api/wham/usage`.
- **Claude**:
  - **OAuth**: Supports interactive OAuth PKCE, Claude Code CLI (`~/.claude/.credentials.json` / keyring), and Claude Desktop `oauth:tokenCacheV2` (`~/.config/Claude/config.json`) against `https://api.anthropic.com/api/oauth/usage` with `429` backoff.
  - **Claude Desktop Cookies**: Decrypts `sessionKey` / `lastActiveOrg` from `~/.config/Claude/Cookies` via Linux SecretService/KDE Wallet.
- **Extensible Architecture**: Modular `UsageProvider` registry and SQLite history store (`~/.local/share/ai-usage-tray/history.db`) with schema ready for future token tracking.

## Quickstart

Run directly:
```bash
python3 -m ai_usage_tray
```

Or install as a `systemd --user` service:
```bash
./install.sh
```
