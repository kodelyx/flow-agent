# 🛠️ Flow Agent API Troubleshooting Guide (error.md)

This document contains standard error scenarios and how to resolve them quickly.

---

## 1. Error: `OSError: [Errno 48] Address already in use`
* **Symptom**: The server fails to start and exits with: `OSError: [Errno 48] Address already in use` (typically for port `8000`, `8100`, or `9222`).
* **Cause**: Another instance of the API server or a background flow-agent process is already running and occupying the port.
* **Resolution**:
  Run the following commands in your terminal to find and kill the process:
  ```bash
  # Clear API server port (8000)
  kill -9 $(lsof -t -i:8000) 2>/dev/null || true

  # Clear Extension bridge HTTP callback port (8100)
  kill -9 $(lsof -t -i:8100) 2>/dev/null || true

  # Clear Extension bridge WebSocket port (9222)
  kill -9 $(lsof -t -i:9222) 2>/dev/null || true
  ```

---

## 2. Error: `Internal Server Error (500) - RuntimeError: curl failed`
* **Symptom**: Step `Uploading Video to Flow` fails with Status Code `500` and the server console prints: `RuntimeError: curl failed: ...`
* **Cause**: Your terminal session has sandbox/proxy environment variables active (`http_proxy`, `https_proxy`, `HTTP_PROXY`, or `HTTPS_PROXY` might be set by the AI agent sandbox). This causes `curl` to route all Google Cloud Storage uploads through a proxy that blocks the connection.
* **Resolution**:
  Clear the proxy variables in your terminal window before starting the server and running the test script:
  ```bash
  # 1. Unset the proxy variables
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

  # 2. Restart the API server in this terminal
  venv/bin/python -m cli.api --port 8000
  ```
  *(Make sure to also run `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY` in your testing/client terminal window as well).*

---

## 3. Error: `Google Flow extension is not connected or unauthorized`
* **Symptom**: `/health` returns `has_flow_key: false` and generation calls return: `"Google Flow extension is not connected or unauthorized. Make sure Google Flow tab is open in Chrome."`
* **Cause**: The Extension Bridge WS is connected, but the Chrome extension is unable to capture the auth token (`flowKey`) because Google Flow is either not open, has gone idle, or your Google account has logged out.
* **Resolution**:
  1. Open Chrome.
  2. Make sure you are logged into your Google account at **[labs.google/fx/tools/flow](https://labs.google/fx/tools/flow)**.
  3. Reload the page. The extension icon in your extension bar should show a green indicator.
  4. Once logged in, the extension will automatically push the token to the server and heal the state.

---

## 4. Error: `Failed (0): TIMEOUT`
* **Symptom**: Request fails after 90 seconds with `TIMEOUT`.
* **Cause**: Google Flow took too long to respond, or there is an active **reCAPTCHA challenge** popped up on your Chrome browser that requires manual verification.
* **Resolution**:
  1. Open Chrome and inspect the Google Flow tab.
  2. If a reCAPTCHA prompt is present, solve it.
  3. Reload the tab to refresh the connection, wait 5 seconds, and try your request again.

---

## 5. Error: `zsh: no such file or directory: venv/bin/python`
* **Symptom**: Running server or test script returns: `zsh: no such file or directory: venv/bin/python` or similar file errors.
* **Cause**: The command is run from the parent workspace folder (`N8N-Agent`) instead of the cloned `flow-agent` project folder where the virtual environment (`venv`) resides.
* **Resolution**:
  Always change your directory to the `flow-agent` folder before running commands, or use the absolute paths:
  ```bash
  # Go to the correct directory first
  cd /path/to/flow-agent
  
  # Then run the command
  venv/bin/python -m cli.api --port 8000
  ```

