#!/usr/bin/env bash
# ============================================================
#  Flow Agent — one-command setup (macOS)
#  Run this ONCE on a new machine:
#      ./setup.sh
#  It will:
#    1. Install the `flow` + `flow-mcp` CLI
#    2. Make the backend auto-start on login (LaunchAgent)
#  MCP is NOT auto-registered — you add it to your own AI client
#  (Claude Desktop / Cursor / Cline / Antigravity...). See MCP.md.
#  Re-running is safe (idempotent).
# ============================================================
set -euo pipefail

GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; BOLD=$'\033[1m'; NC=$'\033[0m'
say()  { printf "%s\n" "${GREEN}✔${NC} $*"; }
warn() { printf "%s\n" "${YELLOW}!${NC} $*"; }
step() { printf "\n%s\n" "${BOLD}▶ $*${NC}"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.flow.agent"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/flow-agent.log"

# ---- 1. Install the CLI -------------------------------------------------
step "1/2  Installing the flow CLI"
if command -v uv >/dev/null 2>&1; then
  ( cd "$HERE" && uv tool install --force . >/dev/null )
  say "Installed with uv"
elif command -v pipx >/dev/null 2>&1; then
  ( cd "$HERE" && pipx install --force . >/dev/null )
  say "Installed with pipx"
else
  warn "Neither 'uv' nor 'pipx' found."
  echo "  Install uv first (one line):"
  echo "     curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "  then re-run ./setup.sh"
  exit 1
fi

# Resolve where the binary actually landed (uv/pipx use ~/.local/bin)
FLOW_BIN="$(command -v flow || true)"
[ -z "$FLOW_BIN" ] && [ -x "$HOME/.local/bin/flow" ] && FLOW_BIN="$HOME/.local/bin/flow"
if [ -z "$FLOW_BIN" ]; then
  warn "flow binary not on PATH yet. Add this to your shell profile:"
  echo '     export PATH="$HOME/.local/bin:$PATH"'
  FLOW_BIN="$HOME/.local/bin/flow"
fi
FLOW_MCP_BIN="$(dirname "$FLOW_BIN")/flow-mcp"
say "flow  → $FLOW_BIN"

# ---- 2. Auto-start on login (LaunchAgent) -------------------------------
step "2/2  Setting up auto-start on login"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# Unload any previous version so we can rewrite cleanly
launchctl unload "$PLIST" >/dev/null 2>&1 || true

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${FLOW_BIN}</string>
        <string>serve</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${HERE}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLISTEOF

launchctl load "$PLIST"
say "Backend will now start automatically on every login"
say "Logs → $LOG"

# ---- Done ---------------------------------------------------------------
printf "\n%s\n" "${BOLD}${GREEN}🎉 All done!${NC}"
cat <<DONE

What happens now:
  • The backend is running and restarts itself on every login.

${BOLD}Next: connect your AI client to the MCP server.${NC}
  The backend does NOT auto-add itself to any client — you paste one
  small config into whatever you use (Claude Desktop / Cursor / Cline /
  Antigravity / etc). Full copy-paste snippets for each are in:

      ${BOLD}MCP.md${NC}

  The command to point your client at is:
      ${FLOW_MCP_BIN}

Before generating, make sure Chrome is open at:
  https://labs.google/fx/tools/flow   (logged in, with the Flow Agent extension)

Handy commands:
  flow status      # is the backend up? extension connected?
  flow credits     # remaining Google Flow credits
  tail -f "$LOG"   # watch backend logs

To stop auto-start:  ./uninstall.sh
DONE
