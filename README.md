# ⚡ Flow Agent

> **Easy-to-use tool to generate images and videos with Google Flow using simple CLI commands, a REST API, or AI assistants like Claude and Cursor.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Server-green)](https://fastapi.tiangolo.com)
[![MCP](https://img.shields.io/badge/MCP-Supported-purple)](https://modelcontextprotocol.io/)
[![Tests](https://img.shields.io/badge/Tests-74%20Passing-brightgreen)](tests/)

---

## 🚀 What is Flow Agent?

Flow Agent connects to Google Flow to create high-quality **AI images and videos**. 

You can use it in 3 ways:
1. **Terminal (CLI)**: Run quick commands like `python main.py image "..."`.
2. **REST API**: Works like an OpenAI server, so any app can connect to it.
3. **MCP Server**: Connect it directly to AI tools like Claude Desktop or Cursor.

---

## ✨ Features

- **Text to Image**: Create images with different sizes (`1:1`, `16:9`, `9:16`, `4:3`, `3:4`).
- **Text to Video**: Make 4s, 6s, 8s, or 10s videos in 720p HD.
- **Image to Video**: Animate any picture into a video.
- **Video Extension**: Continue a video scene smoothly from its last frame.
- **Multi-Account Support**: Add multiple Google accounts to share credits.
- **OpenAI Compatible**: Use your standard OpenAI Python or Node.js code directly.
- **Fast & Reliable**: Direct API calls with automatic retry and local SQLite history.

---

## 📁 Project Structure

```text
flow-agent/
├── bin/
│   └── flow                     # Core Flow engine binary
├── cookies/
│   └── account_<id>.json        # Saved Google account logins
├── data/
│   └── flow.db                  # History database (credits & past jobs)
├── output/
│   └── *.jpg, *.mp4             # Your saved images and videos
│
├── flow_agent/                  # Python code
│   ├── api.py                   # Web API server
│   ├── engine.py                # Bridge to run generations
│   └── mcp_server.py            # AI assistant connector (Claude, Cursor)
│
├── tests/                       # Test files (all 74 tests pass)
├── docs/                        # Detailed guides
├── main.py                      # Main entrypoint
└── pyproject.toml               # Project settings
```

---

## ⚡ Quick Setup (3 Steps)

### Step 1: Install Python Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
```

### Step 2: Connect Your Google Account
1. Open Chrome and load the extension from the `extension/` folder.
2. In terminal, start the bridge:
   ```bash
   ./bin/flow bridge
   ```
3. Open [Google Flow](https://labs.google/fx/tools/flow) in Chrome. Your login will automatically save to `cookies/`.

### Step 3: Check Your Credits
```bash
python main.py balance
```

---

## 🎯 How to Use

### 1. Generate an Image
```bash
python main.py image "a glowing crystal lotus on calm water" --aspect 1:1
```
*Saves to `output/` as a high-quality JPG.*

### 2. Generate a Video
```bash
python main.py video "calm water ripples under a starry sky" --duration 8s --quality 720p
```
*Saves to `output/` as an MP4 video.*

### 3. Turn an Image into Video (Animate)
```bash
python main.py video "camera zooms into the lotus" --start-image output/your-image.jpg
```

### 4. Check Past Generations & Stats
```bash
python main.py stats
```

---

## 🌐 Start the Web API Server

Run the local server:
```bash
python main.py server --port 8001
```

- Open in browser for interactive docs: **`http://127.0.0.1:8001/docs`**

### Use with OpenAI Python SDK:
```python
from openai import OpenAI

# Point to your local Flow Agent server
client = OpenAI(base_url="http://127.0.0.1:8001/v1", api_key="not-needed")

response = client.images.generate(
    prompt="a cute red origami fox sitting in grass",
    size="1024x1024"
)

print("Image URL:", response.data[0].url)
```

---

## 🤖 Connect to Claude Desktop or Cursor (MCP)

Flow Agent can act as a tool for AI assistants so they can generate images and videos for you.

Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "flow": {
      "command": "python3",
      "args": ["/Users/akashyadav/Akash/Flow-Agent-Work/flow-agent/main.py", "mcp"]
    }
  }
}
```

Now you can ask Claude: *"Generate an image of a cyber dragon using Flow"*.

---

## 🧪 Run Tests

To make sure everything is working properly:

```bash
python3 -m pytest -q
```
```text
74 passed in 0.40s
```

---

## 📚 More Details

If you need in-depth information, check the **[`docs/`](docs/)** folder:
- **[API Reference](docs/API_REFERENCE.md)**: Every API endpoint and response.
- **[MCP Guide](docs/MCP_GUIDE.md)**: Setup for Claude, Cursor, and Zed.
- **[CLI Reference](docs/CLI_REFERENCE.md)**: All command flags and shortcuts.
- **[Architecture](docs/ARCHITECTURE.md)**: How the internal system works.

---

## 📄 License

MIT License. Free to use and modify.
