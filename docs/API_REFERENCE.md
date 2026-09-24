# Flow Agent REST API Reference

Complete documentation for the Flow Agent HTTP server.

- **Default Base URL**: `http://127.0.0.1:8001`
- **Interactive Documentation**: `http://127.0.0.1:8001/docs` (Swagger UI) / `http://127.0.0.1:8001/redoc` (ReDoc)

---

## 1. Endpoints Summary

| Method | Endpoint | Category | Description |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/image` | Native | Generate an image using Flow parameters |
| `POST` | `/api/v1/video` | Native | Generate a video (text/image-to-video) |
| `GET` | `/api/v1/balance` | Native | View credits per account (`?refresh=true` for live probe) |
| `GET` | `/api/v1/stats` | Native | View generation history and cumulative metrics |
| `GET` | `/api/v1/projects` | Native | List Google Flow projects for an account |
| `GET` | `/api/v1/media/{filename}` | Native | Fetch generated image or video file |
| `GET` | `/health` | System | Health check and engine binary liveness |
| `POST` | `/v1/images/generations` | OpenAI | OpenAI DALL-E compatible image generation |
| `POST` | `/v1/chat/completions` | OpenAI | OpenAI Chat completion with image output & SSE |

---

## 2. Native Flow Endpoints

### 2.1. Generate Image
`POST /api/v1/image`

Generates one or more images from a text prompt.

#### Request Body
```json
{
  "prompt": "a majestic cybernetic lion in neon rain, 8k cinematic",
  "aspect": "16:9",
  "count": 1,
  "model": "narwhal",
  "all_accounts": false,
  "cookies": null
}
```

- **`prompt`** (string, required): Image prompt description.
- **`aspect`** (string, optional): One of `"1:1"`, `"16:9"`, `"9:16"`, `"4:3"`, `"3:4"`, `"square"`, `"landscape"`, `"portrait"`. Default: `"1:1"`.
- **`count`** (integer, optional): Number of variations (`1` to `4`). Default: `1`.
- **`model`** (string, optional): Flow model key (`"narwhal"`, `"harbor_seal"`). Default: `"narwhal"`.
- **`all_accounts`** (boolean, optional): If `true`, runs generation on every active account in `cookies/` concurrently. Default: `false`.
- **`cookies`** (string, optional): Path to a specific account bundle (e.g., `"cookies/account_c13eea595e47.json"`).

#### Response (`200 OK`)
```json
{
  "job_id": "ba884266-3fe5-4106-89a1-2ed79c8a8e5b",
  "account_id": "acct-e50d5ecec52e",
  "status": "succeeded",
  "files": [
    {
      "path": "/Users/.../flow-agent/output/acct-38e60406-67b.jpg",
      "url": "http://127.0.0.1:8001/api/v1/media/acct-38e60406-67b.jpg"
    }
  ]
}
```

---

### 2.2. Generate Video
`POST /api/v1/video`

Generates a video clip from text or a starting image.

#### Request Body
```json
{
  "prompt": "hyper-lapse of clouds over neon skyscraper city",
  "aspect": "16:9",
  "duration": "8s",
  "quality": "720p",
  "count": 1,
  "start_image": null,
  "all_accounts": false,
  "cookies": null
}
```

- **`duration`** (string, optional): One of `"4s"`, `"6s"`, `"8s"`, `"10s"`. Default: `"8s"`.
- **`quality`** (string, optional): `"360p"` or `"720p"`. Default: `"720p"`.
- **`start_image`** (string, optional): Path to a local image or uploaded media ID to animate.

#### Response (`200 OK`)
```json
{
  "job_id": "5b3f0217-9df1-4658-b806-fe26832a9bb2",
  "account_id": "acct-e50d5ecec52e",
  "status": "succeeded",
  "files": [
    {
      "path": "/Users/.../flow-agent/output/acct-video-1.mp4",
      "url": "http://127.0.0.1:8001/api/v1/media/acct-video-1.mp4"
    }
  ]
}
```

---

### 2.3. Check Balance
`GET /api/v1/balance?refresh=false`

Returns available generation credits across all configured accounts.

- **`refresh`** (boolean query param, default: `false`): When `false`, reads instant cached balances from SQLite without network calls. When `true`, probes upstream Google Flow servers to sync live balances.

#### Response (`200 OK`)
```json
{
  "accounts": [
    {
      "account_id": "acct-1a102fc1d7a1",
      "credits": 50,
      "status": "active"
    },
    {
      "account_id": "acct-e50d5ecec52e",
      "credits": 1049,
      "status": "active"
    }
  ],
  "total_credits": 1099,
  "unread": 0
}
```

---

### 2.4. Generation Statistics
`GET /api/v1/stats`

Returns full generation history and metrics recorded in `data/flow.db`.

#### Response (`200 OK`)
```json
{
  "generations": [
    {
      "job_id": "ba884266-3fe5-4106-89a1-2ed79c8a8e5b",
      "account_id": "acct-e50d5ecec52e",
      "kind": "image",
      "model": "NARWHAL",
      "aspect": "1:1",
      "status": "succeeded",
      "credits_spent": null,
      "elapsed_seconds": 52.901,
      "created_at": "2026-09-24 17:41:51"
    }
  ],
  "totals": {
    "generations": 1,
    "by_status": { "succeeded": 1 },
    "by_kind": { "image": 1 },
    "media_files": 1,
    "credits_spent": 0
  }
}
```

---

### 2.5. Serve Media Asset
`GET /v1/media/{filename}` or `GET /api/v1/media/{filename}`

Streams a generated image or video file directly from `output/`.

- **Security**: Validates that `{filename}` resolves strictly inside `output/`. Path traversal attempts (e.g., `../../etc/passwd`) safely return `404 Not Found`.
- **MIME Types**: Automatically maps `.jpg` / `.jpeg` to `image/jpeg`, `.png` to `image/png`, `.mp4` to `video/mp4`, `.webp` to `image/webp`.

---

## 3. OpenAI-Compatible Endpoints

### 3.1. Images Generations
`POST /v1/images/generations`

Drop-in replacement for OpenAI's `v1/images/generations`. Compatible with standard OpenAI SDKs (`openai.images.generate(...)`).

#### Request Body
```json
{
  "prompt": "futuristic city in autumn, digital art",
  "n": 1,
  "size": "1024x1024",
  "response_format": "url"
}
```

- **`size`** mappings:
  - `"1024x1024"` or `"square"` &rarr; Flow `1:1`
  - `"1792x1024"` or `"landscape"` &rarr; Flow `16:9`
  - `"1024x1792"` or `"portrait"` &rarr; Flow `9:16`
  - `"1365x1024"` &rarr; Flow `4:3`
  - `"1024x1365"` &rarr; Flow `3:4`
- **`response_format`**: `"url"` (default) returns media URLs; `"b64_json"` returns base64-encoded file data.

#### Response (`200 OK`)
```json
{
  "created": 1727201000,
  "data": [
    {
      "url": "http://127.0.0.1:8001/api/v1/media/acct-38e60406-67b.jpg"
    }
  ],
  "flow": {
    "job_id": "ba884266-3fe5-4106-89a1-2ed79c8a8e5b",
    "account_id": "acct-e50d5ecec52e",
    "status": "succeeded"
  }
}
```

---

### 3.2. Chat Completions
`POST /v1/chat/completions`

Generates an image from conversation history, returning a markdown-formatted response or real-time SSE stream.

#### Request Body
```json
{
  "model": "flow",
  "messages": [
    { "role": "system", "content": "You are an AI illustrator." },
    { "role": "user", "content": "Draw an origami hummingbird flying near a blossom" }
  ],
  "stream": false
}
```

#### Response (`200 OK`)
```json
{
  "id": "chatcmpl-flow-ba884266",
  "object": "chat.completion",
  "created": 1727201000,
  "model": "flow",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "![Generated Image](http://127.0.0.1:8001/api/v1/media/acct-38e60406-67b.jpg)"
      },
      "finish_reason": "stop"
    }
  ]
}
```

---

## 4. Client Integration Examples

### Python (OpenAI SDK)
```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8001/v1", api_key="not-needed")

response = client.images.generate(
    prompt="a glowing crystal cavern, unreal engine 5",
    size="1792x1024",
    n=1
)
print("Image URL:", response.data[0].url)
```

### cURL
```bash
curl -X POST http://127.0.0.1:8001/api/v1/image \
  -H "Content-Type: application/json" \
  -d '{"prompt": "emerald dragon over misty mountains", "aspect": "16:9"}'
```
