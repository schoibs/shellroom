# ShellRoom

ShellRoom is an ephemeral, terminal-native group chat app. With ShellRoom, anyone can create a room from their terminal, and easily invite other people to join the same room from their terminals through the generated invite links.

## Package Layout

Might separate server and sdk into separate repos in the future.

```text
shellroom/
  cli/      Typer CLI entrypoint
  sdk/      Async HTTP/WebSocket client
  server/   FastAPI app, room registry, and SQLite storage
  tui/      Textual terminal chat UI
```

## Requirements

- Python 3.12 or newer
- SQLite

## Install

From the repository root:

```sh
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .
```

This installs the `shellroom` console command and the server dependencies.

## Server (needs to be hosted somewhere!)

Start the FastAPI app with Uvicorn:

```sh
python -m uvicorn shellroom.server.app:app --reload
```

The server listens on `http://localhost:8000` by default. You can check it with:

```sh
curl http://localhost:8000/health
```

## Server Configuration

The server reads configuration from environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `SHELLROOM_PUBLIC_URL` | `http://localhost:8000` | Public HTTP URL used in invite links. |
| `SHELLROOM_DATABASE_URL` | `sqlite:///./shellroom.db` | File-backed SQLite database URL. |
| `SHELLROOM_ROOM_ID_LENGTH` | `8` | Generated room ID length, from 6 to 12. |
| `SHELLROOM_MAX_USERS_PER_ROOM` | `25` | Maximum connected users in one room. |
| `SHELLROOM_MAX_ACTIVE_ROOMS` | `1000` | Maximum active room records. |
| `SHELLROOM_MESSAGE_HISTORY_LIMIT` | `100` | Recent messages sent to a joining client. |

Example:

```sh
SHELLROOM_PUBLIC_URL=https://chat.example.com \
SHELLROOM_DATABASE_URL=sqlite:///./data/shellroom.db \
python -m uvicorn shellroom.server.app:app --host 0.0.0.0 --port 8000
```

## Create A Room

In another terminal, create a room and open the chat UI:

```sh
shellroom create --name Alice
```

ShellRoom displays the room ID, invite URL, and join command in the UI header.

## Join A Room

Use the join command from the room creator's terminal:

```sh
shellroom join ROOM_ID --name Bob
```

If your server is not running at `http://localhost:8000`, pass the server URL:

```sh
shellroom create --server https://chat.example.com --name Alice
shellroom join ROOM_ID --server https://chat.example.com --name Bob
```

## CLI Commands

```sh
shellroom create [--name NAME] [--server URL]
shellroom join ROOM_ID [--name NAME] [--server URL]
shellroom version
```

If `--name` is omitted, ShellRoom uses your system username. Display names are
trimmed to 24 characters.
