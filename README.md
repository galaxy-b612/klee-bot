# Klee Core

Unified message dispatch layer for Klee bot (QQ group robot).

**Phase 1 — Message Intake & Normalization**

- Receives OneBot v11 events from NapCat via HTTP
- Normalizes all messages into a unified Message model
- Stores messages + attachments in SQLite
- Forwards events to downstream services (AstrBot, Yunzai, Hermes, Meme)

## Architecture

```
QQ群A → NapCat-1
QQ群B → NapCat-2
QQ群C → NapCat-3
        ↓
      Klee Core (port 8000)
        ↓
┌──────────┬──────────┬────────────┬──────────┐
│ AstrBot  │ Yunzai   │ Hermes     │ Meme     │
│ 插件执行  │ 插件执行  │ 拟人回复    │ 表情管理  │
└──────────┴──────────┴────────────┴──────────┘
```

## Quick Start

### Prerequisites

- Python 3.11+
- pip / uv

### Local Development

```bash
# Clone and enter project
cd klee_core

# Install dependencies
pip install -e ".[dev]"

# Configure bots
# Edit config/bots.yaml with your NapCat self_ids and group IDs

# Run
python -m app.main

# Or with uvicorn
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Docker

```bash
# Build and run
docker-compose up -d

# Check health
curl http://localhost:8000/health
```

## Configuration

### `config/bots.yaml`

Define your bot instances and managed groups:

```yaml
bots:
  - bot_id: "klee_main"
    self_id: 1432028231          # QQ number
    napcat_name: "napcat_main"
    groups:
      - 915443332                 # Managed group IDs
    downstream:
      astrbot_url: "http://127.0.0.1:6198"
      yunzai_url: "http://127.0.0.1:2536"
      hermes_url: "http://127.0.0.1:18800"
      meme_url: "http://127.0.0.1:8700"
    enabled: true
```

### `config/routes.yaml`

Routing strategy and timeout settings:

```yaml
routing:
  strategy: "parallel"       # parallel | ordered
  timeout_seconds: 10
  enabled_endpoints:
    - astrbot
    - yunzai
    - hermes
    - meme
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/onebot/event` | Receive OneBot v11 message event |
| `GET` | `/health` | Health check |
| `GET` | `/messages/recent?group_id=xxx&limit=50` | Recent messages for a group |

### POST /onebot/event

Receives a OneBot v11 message event from NapCat:

```json
{
  "self_id": 1432028231,
  "post_type": "message",
  "message_type": "group",
  "group_id": 915443332,
  "user_id": 123456789,
  "message": [
    {"type": "text", "data": {"text": "Hello, Klee!"}}
  ],
  "sender": {
    "user_id": 123456789,
    "nickname": "User"
  }
}
```

**Response** (200):
```json
{
  "status": "ok",
  "message_id": 1,
  "message_type": "text",
  "bot_id": "klee_main"
}
```

## Supported Message Types

| Type | Description | Normalized As |
|------|-------------|---------------|
| `text` | Plain text / emoji | `text` |
| `image` | Image only | `image` |
| `record` | Voice/audio | `voice` |
| `video` | Video | `video` |
| `file` | File attachment | `file` |
| `text + image` | Image with caption | `mixed` |
| `reply` | Quoted reply | `reply` |
| `forward` | Forwarded messages | `forward` |

**Note**: Images, voice, video, and files are recorded with metadata only (no deep analysis in Phase 1).

## Database

SQLite database stored at `data/klee.db` (configurable via `KLEE_CORE_DATABASE_URL`).

### Tables

- **bot_instances** — bot-to-group mappings with downstream endpoints
- **messages** — unified message records (normalized from OneBot events)
- **attachments** — metadata for media files, emoji, forwarded content

## Testing

```bash
# Run all tests
pytest app/tests/ -v

# Run specific test file
pytest app/tests/test_onebot.py -v

# Run with coverage
pytest app/tests/ --cov=app --cov-report=term-missing
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KLEE_CORE_CONFIG_DIR` | `config/` (project-relative) | Path to config directory |
| `KLEE_CORE_DATABASE_URL` | `sqlite+aiosqlite:///data/klee.db` | Database connection URL |

## License

MIT
