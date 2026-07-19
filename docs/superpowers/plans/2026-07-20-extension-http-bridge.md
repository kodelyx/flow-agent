# Flow-Agent 扩展 HTTP/SSE 桥接改造实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**工作目录（唯一）：** `F:\Code\Flow-Agent-New`

**目标：** 把 Flow-Agent 浏览器插件与本地后端之间的通信，从依赖 `ws://127.0.0.1:8001/ws` 的 WebSocket，改造成以 HTTP 为主（轮询 + 可选 SSE）的双向桥接，从而在 Hubstudio / AdsPower 等指纹浏览器里稳定连通。

**架构：** 保留现有“后端发命令、扩展代打 Google Flow、结果回传”的消息模型，但把传输层换成：扩展主动 `POST /api/ext/hello` 注册并上报 token；扩展每 1–2 秒 `GET /api/ext/poll` 拉取待执行命令；扩展用 `POST /api/ext/callback` 回传结果；可选再加 `GET /api/ext/events` SSE 作为低延迟下行。WebSocket 作为兼容回退，不删除。

**技术栈：** Chrome MV3 扩展（`background.js`）、FastAPI、现有 `omniflash/bridge.py` 消息路由、pytest、chrome.alarms / fetch

---

## 仓库布局（以本目录为准）

```text
F:\Code\Flow-Agent-New\
├── flow-agent\                 # Python 后端 / CLI
│   ├── cli\api.py
│   ├── omniflash\bridge.py
│   ├── omniflash\config.py
│   ├── flow_cli\main.py
│   ├── tests\
│   └── config.env
├── flow-chrome-extension\      # 浏览器插件
│   ├── background.js
│   ├── manifest.json
│   └── ...
├── release\                    # 预编译二进制（本改造以源码为准）
├── docs\superpowers\plans\     # 本计划
├── README.md
└── MCP.md
```

> 说明：该目录最初只有扩展与 release；已将后端源码补齐到 `flow-agent\`。后续所有修改、测试、commit 都只在 `F:\Code\Flow-Agent-New` 进行，不再改 Cloak/Hubstudio 旧路径。

---

## 背景与约束（实现前必读）

### 现状链路
1. 后端 `flow serve` 监听 `http://127.0.0.1:8001`
2. 扩展连接 `ws://127.0.0.1:8001/ws`
3. 后端通过 WS 下发：`api_request` / `trpc_request` / `upload_video` / `solve_captcha` / `get_status` / `open_flow_tab` / `refresh_flow_tab`
4. 扩展抓到 token 后发 `token_captured` / `extension_ready`
5. 带 `id` 的响应已可走 HTTP outbox：`POST callbackUrl`
6. 嗅探请求曾硬编码：`POST http://127.0.0.1:8100/api/ext/callback`

### 指纹浏览器实测结论
| 浏览器 | 本地 HTTP | 本地 WebSocket | token |
|--------|-----------|----------------|-------|
| 官方 Chrome | 通 | 通 | 有 |
| AdsPower | 通 | 不通 / 秒断 | 有 |
| Hubstudio | 插件侧常失败 | 失败 | 有 |

因此：**不能再把“扩展已连接”定义为“WS 对象存在”。**  
应改为：**最近一次 hello/poll 在线 + 持有 flowKey。**

### 设计原则
1. **HTTP 优先，WS 回退**（`transport=http|ws|auto`，默认 `auto`）
2. **命令队列在后端**，扩展只做拉取/执行/回传
3. **幂等**：同一 `id` 的命令与响应可重试，不重复生效
4. **最小改动**：复用 `bridge._pending` / `_route_response` / outbox
5. **端口统一到 8001**：不再默认写死 8100
6. **manifest 补齐** `http://127.0.0.1:8001/*` host_permissions

### 协议草案

#### A. 扩展 → 后端

`POST /api/ext/hello`
```json
{
  "type": "hello",
  "session_id": "uuid-or-stable-id",
  "extension_version": "1.0.0",
  "flowKeyPresent": true,
  "flowKey": "ya29....",
  "capabilities": ["api_request", "trpc_request", "upload_video", "solve_captcha"]
}
```
响应：
```json
{
  "ok": true,
  "session_id": "...",
  "secret": "callback-bearer",
  "callback_url": "http://127.0.0.1:8001/api/ext/callback",
  "poll_url": "http://127.0.0.1:8001/api/ext/poll",
  "poll_interval_ms": 1000,
  "events_url": "http://127.0.0.1:8001/api/ext/events"
}
```

`GET /api/ext/poll?session_id=...`
- 鉴权：`Authorization: Bearer <secret>`
- 响应：
```json
{
  "ok": true,
  "commands": [
    {
      "id": "req-uuid",
      "method": "api_request",
      "params": { "...": "..." }
    }
  ],
  "server_time": 1784488000000
}
```

`POST /api/ext/callback`（已有，增强）
- 继续接收命令结果、`token_captured`、`extension_ready`、`ping`、`media_urls_refresh`
- 必须支持 Bearer secret

#### B. 后端 → 扩展
仍使用现有 method，只是 `send_message()` 在 HTTP 模式下写入命令队列。

#### C. 连接健康定义
```python
extension_connected = session_last_seen_within(15s)
has_flow_key = bool(flow_key)
healthy = extension_connected and has_flow_key
```

---

## 文件结构

### 将创建
- `flow-agent/omniflash/http_bridge.py`
- `flow-agent/tests/test_http_bridge.py`
- `flow-agent/tests/test_ext_http_api.py`
- `docs/superpowers/plans/2026-07-20-extension-http-bridge.md`（本文件）

### 将修改
- `flow-chrome-extension/manifest.json`
- `flow-chrome-extension/background.js`
- `flow-agent/omniflash/bridge.py`
- `flow-agent/cli/api.py`
- `flow-agent/omniflash/config.py`（如需）
- `flow-agent/config.env`
- `README.md`

### 不改
- Google Flow 生成业务逻辑
- OpenAI 兼容外部 API 形状
- MCP `/sse`（那是 MCP 客户端通道）

---

### 任务 0：工作区确认与基线

**文件：**
- 工作目录：`F:\Code\Flow-Agent-New`

- [x] **步骤 1：确认目录**

```powershell
cd F:\Code\Flow-Agent-New
dir
Test-Path .\flow-agent\omniflash\bridge.py
Test-Path .\flow-chrome-extension\background.js
```

预期：两者均为 `True`

- [x] **步骤 2：安装/确认 Python 依赖（在本仓库）**

```powershell
cd F:\Code\Flow-Agent-New\flow-agent
# 优先使用 uv 或现有 venv；示例如下
python -m pip install -e . -q
python -c "import omniflash, cli.api; print('ok')"
```

- [x] **步骤 3：基线状态**

```powershell
cd F:\Code\Flow-Agent-New
git status
git rev-parse --show-toplevel
```

预期：toplevel 为 `F:/Code/Flow-Agent-New` 或该仓库根

---

### 任务 1：后端 HTTP session + 命令队列模型

**文件：**
- 创建：`flow-agent/omniflash/http_bridge.py`
- 修改：`flow-agent/omniflash/bridge.py`（仅预留接入点，可放任务 2）
- 测试：`flow-agent/tests/test_http_bridge.py`

- [x] **步骤 1：编写失败测试**

```python
# flow-agent/tests/test_http_bridge.py
import time
from omniflash.http_bridge import ExtensionHttpRegistry

def test_hello_registers_session_and_accepts_token():
    reg = ExtensionHttpRegistry(session_ttl_sec=15)
    out = reg.hello(session_id="s1", flow_key="tok", secret="test-secret")
    assert out["ok"] is True
    assert reg.is_connected("s1") is True
    assert reg.get_flow_key("s1") == "tok"

def test_enqueue_and_poll_returns_command_once():
    reg = ExtensionHttpRegistry(session_ttl_sec=15)
    reg.hello(session_id="s1", flow_key="tok", secret="sec")
    reg.enqueue("s1", {"id": "r1", "method": "get_status", "params": {}})
    first = reg.poll("s1")
    second = reg.poll("s1")
    assert len(first["commands"]) == 1
    assert first["commands"][0]["id"] == "r1"
    assert second["commands"] == []

def test_session_expires():
    reg = ExtensionHttpRegistry(session_ttl_sec=1)
    reg.hello(session_id="s1", flow_key="tok", secret="sec")
    time.sleep(1.2)
    assert reg.is_connected("s1") is False
```

- [x] **步骤 2：运行测试确认失败**

```powershell
cd F:\Code\Flow-Agent-New\flow-agent
python -m pytest tests/test_http_bridge.py -v
```

预期：FAIL，找不到 `ExtensionHttpRegistry`

- [x] **步骤 3：实现最小 registry**

`flow-agent/omniflash/http_bridge.py` 需包含：
- `hello(session_id, flow_key, secret, meta=None)`
- `touch(session_id)`
- `is_connected(session_id=None)`
- `has_online_session()`
- `enqueue(session_id|None, command)`
- `poll(session_id, max_commands=10)`
- `get_flow_key(session_id=None)`
- 线程安全（`threading.Lock`）

```python
@dataclass
class Session:
    session_id: str
    secret: str
    flow_key: str | None
    last_seen: float
    queue: deque
```

- [x] **步骤 4：运行测试确认通过**

```powershell
cd F:\Code\Flow-Agent-New\flow-agent
python -m pytest tests/test_http_bridge.py -v
```

- [x] **步骤 5：Commit**

```powershell
cd F:\Code\Flow-Agent-New
git add flow-agent/omniflash/http_bridge.py flow-agent/tests/test_http_bridge.py
git commit -m "feat(bridge): add HTTP extension session registry and command queue"
```

---

### 任务 2：FastAPI 增加 hello/poll，并增强 callback/health

**文件：**
- 修改：`flow-agent/cli/api.py`
- 修改：`flow-agent/omniflash/bridge.py`
- 测试：`flow-agent/tests/test_ext_http_api.py`

- [x] **步骤 1：写 API 级失败测试**

```python
# flow-agent/tests/test_ext_http_api.py
from fastapi.testclient import TestClient

def test_hello_and_poll_roundtrip():
    from cli.api import app, bridge
    client = TestClient(app)
    r = client.post("/api/ext/hello", json={
        "session_id": "s-test",
        "flowKeyPresent": True,
        "flowKey": "tok-1",
        "extension_version": "1.0.0",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    secret = body["secret"]
    bridge.enqueue_http_command({"id": "c1", "method": "get_status", "params": {}})
    p = client.get(
        "/api/ext/poll",
        params={"session_id": "s-test"},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert p.status_code == 200
    assert p.json()["commands"][0]["id"] == "c1"
```

- [x] **步骤 2：运行测试确认失败**

```powershell
cd F:\Code\Flow-Agent-New\flow-agent
python -m pytest tests/test_ext_http_api.py -v
```

- [x] **步骤 3：实现端点与 bridge 接入**

1. `POST /api/ext/hello`
2. `GET /api/ext/poll`
3. 增强 `POST /api/ext/callback`
4. `/health` 增加：
```python
"extension_connected": bridge.is_extension_connected(),
"has_flow_key": bridge._flow_key is not None,
"transport": bridge.active_transport(),  # http | ws | none
```

`bridge.send_message`：
```python
async def send_message(self, msg):
    if self.http_registry and self.http_registry.has_online_session():
        self.http_registry.enqueue(None, msg)
        return
    # fallback existing WS send
```

`api_request` 前置：
```python
if not self.is_extension_connected():
    return {"error": "Extension not connected"}
```

- [x] **步骤 4：测试通过**

```powershell
cd F:\Code\Flow-Agent-New\flow-agent
python -m pytest tests/test_http_bridge.py tests/test_ext_http_api.py -v
```

- [x] **步骤 5：Commit**

```powershell
cd F:\Code\Flow-Agent-New
git add flow-agent/cli/api.py flow-agent/omniflash/bridge.py flow-agent/tests/test_ext_http_api.py
git commit -m "feat(api): add extension hello/poll HTTP endpoints and health transport"
```

---

### 任务 3：扩展 manifest + HTTP transport

**文件：**
- 修改：`flow-chrome-extension/manifest.json`
- 修改：`flow-chrome-extension/background.js`

- [x] **步骤 1：更新 host_permissions**

```json
"host_permissions": [
  "https://labs.google/*",
  "https://aisandbox-pa.googleapis.com/*",
  "https://aisandbox-pa.sandbox.googleapis.com/*",
  "https://storage.googleapis.com/*",
  "http://127.0.0.1:8001/*",
  "http://localhost:8001/*",
  "http://127.0.0.1:8100/*"
]
```

- [x] **步骤 2：HTTP 优先常量**

```javascript
const AGENT_BASE = 'http://127.0.0.1:8001';
const AGENT_WS_URL = 'ws://127.0.0.1:8001/ws';
const AGENT_HELLO_URL = `${AGENT_BASE}/api/ext/hello`;
const AGENT_POLL_URL = `${AGENT_BASE}/api/ext/poll`;
const AGENT_CALLBACK_URL = `${AGENT_BASE}/api/ext/callback`;
const TRANSPORT_MODE = 'auto'; // auto | http | ws
```

- [x] **步骤 3：实现 `connectViaHttp()` / poll loop / `handleAgentMessage()`**

- [x] **步骤 4：`sendToAgent` 优先 HTTP callback + Bearer**

- [x] **步骤 5：`connectToAgent()` auto 模式：HTTP 成功则不强制 WS**

- [x] **步骤 6：popup `connected` 判定包含 http session**

- [x] **步骤 7：sniff 转发改用 `callbackUrl || AGENT_CALLBACK_URL`**

- [x] **步骤 8：手动验证**

```powershell
cd F:\Code\Flow-Agent-New\flow-agent
python -m flow_cli serve
# 另开终端
curl http://127.0.0.1:8001/health
```

官方 Chrome 加载：`F:\Code\Flow-Agent-New\flow-chrome-extension`

期望 health：
```json
{"extension_connected": true, "has_flow_key": true, "transport": "http"}
```

- [x] **步骤 9：Commit**

```powershell
cd F:\Code\Flow-Agent-New
git add flow-chrome-extension/manifest.json flow-chrome-extension/background.js
git commit -m "feat(extension): prefer HTTP hello/poll bridge over WebSocket"
```

---

### 任务 4：配置与文档

**文件：**
- 修改：`flow-agent/omniflash/config.py`
- 修改：`flow-agent/config.env`
- 修改：`README.md`

- [x] **步骤 1：配置项**

```env
EXT_TRANSPORT=auto
EXT_SESSION_TTL_SEC=20
EXT_POLL_INTERVAL_MS=1000
ENABLE_EXTENSION_WS=1
```

- [x] **步骤 2：README 增加“指纹浏览器 / HTTP 桥”说明**

- [x] **步骤 3：Commit**

```powershell
cd F:\Code\Flow-Agent-New
git add flow-agent/omniflash/config.py flow-agent/config.env README.md
git commit -m "docs: document HTTP extension transport for fingerprint browsers"
```

---

### 任务 5：端到端验证矩阵

- [x] **步骤 1：自动化**

```powershell
cd F:\Code\Flow-Agent-New\flow-agent
python -m pytest tests/test_http_bridge.py tests/test_ext_http_api.py -v
```

- [ ] **步骤 2：官方 Chrome 回归**

- [ ] **步骤 3：Hubstudio 验证（需本地 HTTP 可达）**

- [ ] **步骤 4：AdsPower 验证**

- [ ] **步骤 5：必要时修复后最终 commit**

```powershell
cd F:\Code\Flow-Agent-New
git add -A
git commit -m "fix: stabilize HTTP extension bridge for fingerprint browsers"
```

---

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| MV3 SW 休眠 | 命令延迟 | alarms 保活 + 后端队列 |
| 指纹浏览器连 HTTP 也拦 | 仍不可用 | 记录环境限制；后续 LAN IP / 隧道附加计划 |
| HTTP+WS 双投递 | 重复生成 | 命令 id 幂等 + 单一 active transport |
| 旧扩展只支持 WS | 兼容 | 保留 `/ws` |

## 不在本计划内
1. 公网 WSS / Cloudflare Tunnel
2. 默认绑定 `0.0.0.0`（可另开附加任务）
3. 账号池 / OpenAI API 重写
4. 自动改 Hubstudio/AdsPower 代理

## 成功标准
1. 官方 Chrome：HTTP 模式连通并可完成 credits/生成
2. 至少一个指纹浏览器：在本地 HTTP 可达时不依赖 WS 也能连通
3. `/health` 显示 `transport`
4. `EXT_TRANSPORT=ws` 仍可用

## 建议工期
半天到 1 天：任务 1–2（后端）→ 任务 3（扩展）→ 任务 4–5（文档与验证）

---

## 自检
1. 规格覆盖：HTTP 轮询替代 WS 下行 + callback 回传 + 健康状态重定义
2. 工作目录已锁定到 `F:\Code\Flow-Agent-New`
3. 关键路径均相对本仓库，不再引用旧 Cloak/Hubstudio 路径
