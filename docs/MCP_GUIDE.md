# Flow Agent MCP Server Guide

Complete integration guide for connecting **Flow Agent** to AI assistants (Claude Desktop, Cursor, Antigravity, Zed, Cline) using the **Model Context Protocol (MCP)**.

---

## 1. Overview

Flow Agent implements the MCP standard (`protocolVersion: 2024-11-05`) over **JSON-RPC 2.0**. It exposes five native generation, balance, and asset tools to LLM agents.

The MCP server supports two transport mechanisms:
1. **Stdio (Standard Input / Output)**: Used by local desktop clients such as Claude Desktop, Cursor, and IDE extensions.
2. **SSE (Server-Sent Events over HTTP)**: Used by web applications and distributed agent systems.

---

## 2. Quick Setup in AI Clients

### 2.1. Claude Desktop Setup
Open your Claude Desktop configuration file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Add the `flow-agent` entry:

```json
{
  "mcpServers": {
    "flow": {
      "command": "python3",
      "args": [
        "/Users/akashyadav/Akash/Flow-Agent-Work/flow-agent/main.py",
        "mcp"
      ]
    }
  }
}
```

Restart Claude Desktop. You will see the **Flow tools** available in Claude's tool picker.

---

### 2.2. Cursor / Antigravity / Cline Setup
In your IDE settings or MCP configuration:

```json
{
  "name": "flow-agent",
  "command": "python3",
  "args": ["/Users/akashyadav/Akash/Flow-Agent-Work/flow-agent/main.py", "mcp"],
  "transport": "stdio"
}
```

---

### 2.3. SSE (HTTP) Transport
To run the MCP server over HTTP Server-Sent Events (default port `8002`):

```bash
python main.py mcp --sse --port 8002
```

- **SSE Stream Endpoint**: `GET http://127.0.0.1:8002/sse`
- **RPC Message Endpoint**: `POST http://127.0.0.1:8002/messages`

---

## 3. Available Tools Reference

### 3.1. `flow_generate_image`
Generates an image from a prompt on Google Flow.

#### Parameters:
| Parameter | Type | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `prompt` | `string` | **Yes** | — | Description of the scene or subject to draw. |
| `aspect` | `string` | No | `"1:1"` | Aspect ratio: `"1:1"`, `"16:9"`, `"9:16"`, `"4:3"`, `"3:4"`, `"square"`, `"landscape"`, `"portrait"`. |
| `count` | `integer` | No | `1` | Number of variations (1 to 4). |
| `model` | `string` | No | `"narwhal"` | Engine model: `"narwhal"`, `"harbor_seal"`. |
| `all_accounts`| `boolean`| No | `false` | Distribute the prompt across all signed-in accounts at once. |

#### Example Tool Call:
```json
{
  "name": "flow_generate_image",
  "arguments": {
    "prompt": "futuristic flying supercar over neo tokyo skyline at dusk, 8k",
    "aspect": "16:9"
  }
}
```

---

### 3.2. `flow_generate_video`
Generates an animated video clip from text or an image.

#### Parameters:
| Parameter | Type | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `prompt` | `string` | **Yes** | — | Video action or scene description. |
| `aspect` | `string` | No | `"16:9"` | `"16:9"`, `"9:16"`, `"landscape"`, `"portrait"`. |
| `duration` | `string` | No | `"8s"` | Duration: `"4s"`, `"6s"`, `"8s"`, `"10s"`. |
| `quality` | `string` | No | `"720p"` | `"720p"` or `"360p"`. |
| `start_image` | `string` | No | `null` | Local image path or uploaded media ID to animate. |

---

### 3.3. `flow_get_balance`
Retrieves credit balances across all registered accounts.

#### Parameters:
| Parameter | Type | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `refresh` | `boolean` | No | `false` | If `true`, sends live probes to Google Flow servers. If `false`, returns instant SQLite cached values. |

---

### 3.4. `flow_get_stats`
Reports total historical generations, success/failure counts, and credits spent.

#### Parameters:
*No parameters required.*

---

### 3.5. `flow_upload_video`
Uploads a local video file and registers it as a reusable media ID for video editing.

#### Parameters:
| Parameter | Type | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `path` | `string` | **Yes** | — | Absolute or relative path to the local video file. |

---

## 4. MCP Testing & Verification

You can test the MCP server directly using Python:

```python
import asyncio
from flow_agent.mcp_server import handle_message

async def test():
    req = '{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}'
    response = await handle_message(req)
    print("MCP Tools Response:", response)

asyncio.run(test())
```
