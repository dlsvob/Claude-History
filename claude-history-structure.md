# Claude Code Conversation History Structure

Documentation of how Claude Code stores conversation history and session data under `~/.claude/`.

## Top-Level Directory Structure

| Path | Purpose |
|---|---|
| `history.jsonl` | Global index of all user inputs across all sessions |
| `projects/` | Per-project conversation data |
| `file-history/` | Versioned file snapshots for undo capability |
| `paste-cache/` | Large pasted content, stored by content hash |
| `tasks/` | Session lock files and task watermarks |
| `todos/` | Task list JSON files per session |
| `plans/` | Markdown plan documents |
| `debug/` | Per-session debug/error logs |
| `shell-snapshots/` | Captured zsh environment (functions, aliases, env vars) |
| `stats-cache.json` | Aggregated usage statistics (tokens, costs, sessions) |
| `settings.json` / `settings.local.json` | Permissions and configuration |

## Global History Index (`history.jsonl`)

One JSON object per line, one entry per user input. Serves as a quick lookup of what was said, when, and in which project/session.

```json
{
  "display": "the user's input text",
  "pastedContents": {},
  "timestamp": 1770826134822,
  "project": "/path/to/project",
  "sessionId": "uuid"
}
```

| Field | Description |
|---|---|
| `display` | The user's input text |
| `pastedContents` | Object mapping paste IDs to content hashes |
| `timestamp` | Unix timestamp in milliseconds |
| `project` | Absolute path to the project directory |
| `sessionId` | UUID linking to the full conversation file |

## Per-Project Conversations (`projects/<encoded-path>/`)

Each project gets a directory named by encoding its absolute path (e.g. `-home-user-Dev-MyApp/`).

### Files within a project directory

| Path | Description |
|---|---|
| `<sessionId>.jsonl` | Main conversation transcript |
| `<sessionId>/subagents/agent-<agentId>.jsonl` | Subagent (Task tool) conversation threads |
| `<sessionId>/tool-results/<resultId>.txt` | Large tool outputs stored separately |

### Message Types

Each line in a session JSONL file is a self-contained JSON object. The `type` field determines the entry kind.

#### User Messages (`type: "user"`)

```json
{
  "type": "user",
  "parentUuid": null,
  "uuid": "030cb37e-...",
  "sessionId": "740fea04-...",
  "cwd": "/home/user/project",
  "gitBranch": "main",
  "version": "2.1.49",
  "permissionMode": "default",
  "userType": "external",
  "isSidechain": false,
  "message": {
    "role": "user",
    "content": "the user's input text"
  },
  "timestamp": "2026-02-20T11:59:09.485Z",
  "todos": []
}
```

#### Assistant Messages (`type: "assistant"`)

```json
{
  "type": "assistant",
  "parentUuid": "030cb37e-...",
  "uuid": "3ffd0c3f-...",
  "sessionId": "740fea04-...",
  "cwd": "/home/user/project",
  "isSidechain": false,
  "message": {
    "model": "claude-opus-4-6",
    "id": "msg_016rax...",
    "role": "assistant",
    "content": [
      { "type": "text", "text": "..." }
    ],
    "stop_reason": "end_turn",
    "usage": {
      "input_tokens": 3,
      "cache_creation_input_tokens": 1035,
      "cache_read_input_tokens": 18659,
      "output_tokens": 11
    }
  },
  "timestamp": "2026-02-20T11:59:12.075Z"
}
```

#### File History Snapshots (`type: "file-history-snapshot"`)

Records the state of tracked files at a given point in the conversation, enabling undo/restore.

```json
{
  "type": "file-history-snapshot",
  "messageId": "030cb37e-...",
  "snapshot": {
    "messageId": "030cb37e-...",
    "trackedFileBackups": {},
    "timestamp": "2026-02-20T11:59:09.485Z"
  }
}
```

#### Queue Operations (`type: "queue-operation"`)

Task queue events for managing background work.

### Common Message Fields

| Field | Description |
|---|---|
| `uuid` | Unique identifier for this message |
| `parentUuid` | UUID of the preceding message (forms the conversation chain) |
| `type` | `"user"`, `"assistant"`, `"file-history-snapshot"`, `"queue-operation"` |
| `sessionId` | Session this message belongs to |
| `cwd` | Working directory at time of message |
| `gitBranch` | Active git branch |
| `timestamp` | ISO 8601 timestamp |
| `isSidechain` | Whether this is part of parallel subagent work |

### Conversation Threading

Messages are linked via `parentUuid` → `uuid`, forming a chain:

```
User message (parentUuid: null, uuid: A)
  └─ Assistant message (parentUuid: A, uuid: B)
      └─ User message (parentUuid: B, uuid: C)
          └─ Assistant message (parentUuid: C, uuid: D)
```

## File History (`file-history/<sessionId>/`)

Stores versioned copies of files edited during a session.

Files are named using a hash of the file path plus a version number:

```
04d44d440cd31e2c@v1    # First version (before edit)
04d44d440cd31e2c@v2    # Second version (after edit)
04d44d440cd31e2c@v3    # Third version (after another edit)
```

These are full file copies, enabling restore to any point in the session.

## Paste Cache (`paste-cache/`)

Large pasted content (logs, curl output, code dumps) is stored as text files named by content hash:

```
paste-cache/
  3d770eaeb7bbc4b1.txt    (6.5 KB)
  a1b2c3d4e5f67890.txt    (14 KB)
```

Referenced from `history.jsonl` entries via the `pastedContents` field, avoiding inline duplication.

## Subagent Architecture (`projects/<project>/<sessionId>/subagents/`)

When the Task tool spawns parallel agents, each gets its own JSONL file:

```
subagents/
  agent-a0f0599.jsonl
  agent-b1c2d3e.jsonl
```

Same JSONL format as the main conversation but isolated to that agent's scope.

## Debug Logs (`debug/<sessionId>.txt`)

Plain text logs with timestamps and severity levels:

```
2026-02-11T16:06:21.571Z [ERROR] Failed to save config with lock
2026-02-11T16:06:21.573Z [DEBUG] Writing to temp file
2026-02-11T16:06:21.578Z [DEBUG] Loaded 1 unique skills
```

## Shell Snapshots (`shell-snapshots/`)

Captures of the shell environment (zsh functions, aliases, environment variables) used to restore context for Bash tool calls:

```
shell-snapshots/
  snapshot-zsh-1771588772590-m0x0qx.sh    (16 KB)
```

## Plans (`plans/`)

Markdown documents with auto-generated names storing technical plans and architectural decisions:

```
plans/
  scalable-bubbling-pine.md
```

## Usage Statistics (`stats-cache.json`)

Aggregated usage data:

```json
{
  "version": 2,
  "lastComputedDate": "2026-02-16",
  "totalSessions": 23,
  "totalMessages": 11803,
  "modelUsage": {
    "claude-opus-4-6": {
      "inputTokens": 334269,
      "outputTokens": 287099,
      "cacheReadInputTokens": 294643533
    }
  }
}
```

## Design Patterns

- **JSONL format** — Enables streaming and incremental parsing; each line is self-contained
- **UUID linking** — `parentUuid` → `uuid` chains form the conversation tree
- **Separation of concerns** — Large tool outputs go to `tool-results/`, pasted content to `paste-cache/`, keeping the main JSONL compact
- **Session scoping** — Everything is organized by session ID so each conversation is isolated
- **Subagent isolation** — Parallel Task tool work gets its own JSONL file under `subagents/`
- **Versioned file tracking** — Full file copies at each edit point enable precise undo
