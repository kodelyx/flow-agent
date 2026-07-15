#!/usr/bin/env bash
# Stops auto-start and removes the LaunchAgent. Leaves the CLI installed.
set -euo pipefail
LABEL="com.flow.agent"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "✔ Auto-start removed. The backend will no longer start on login."
echo "  (The 'flow' CLI is still installed — run 'uv tool uninstall flow-agent' to remove it.)"
echo "  (Remove the 'flow' MCP entry from your AI client's config yourself — see MCP.md.)"
