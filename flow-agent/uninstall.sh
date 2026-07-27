#!/usr/bin/env bash
# Stops auto-start and removes the LaunchAgent. Leaves the CLI installed.
set -euo pipefail
LABEL="com.flow.agent"
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✔ macOS LaunchAgent removed."
elif [ "$OS" = "Linux" ]; then
  SERVICE_FILE="$HOME/.config/systemd/user/flow-agent.service"
  systemctl --user stop flow-agent.service 2>/dev/null || true
  systemctl --user disable flow-agent.service 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl --user daemon-reload
  echo "✔ Linux systemd service removed."
else
  echo "! OS not recognized for autostart removal."
fi

echo "✔ Auto-start removed. The backend will no longer start on login."
echo "  (The 'flow' CLI is still installed — run 'uv tool uninstall flow-agent' to remove it.)"
echo "  (Remove the 'flow' MCP entry from your AI client's config yourself — see MCP.md.)"
