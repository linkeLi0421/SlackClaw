# Memory System

SlackClaw includes a persistent memory system that lets AI agents recall facts, preferences, and procedures across conversations. Memories survive thread boundaries and app restarts.

## Overview

The memory system has two layers:

1. **Thread context** (ephemeral) -- per-thread conversation history shared between agents in the same Slack thread. Stored in SQLite only; no files on disk.
2. **Persistent memory** (durable) -- cross-thread facts stored as human-readable Markdown files and indexed with SQLite FTS5 for fast full-text search.

Both layers are visible on the local dashboard.

## Enabling Memory

Set `MEMORY_ENABLED=true` in your config (setup UI or `.env`). All other memory settings are optional.

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_ENABLED` | `false` | Enable the persistent memory system |
| `MEMORY_MAX_PER_SCOPE` | `100` | Max memories per scope key |
| `MEMORY_RETENTION_DAYS` | `90` | Auto-purge unaccessed memories after this many days |
| `MEMORY_INJECTION_MAX_CHARS` | `2000` | Max characters of memory context injected into agent prompts |
| `MEMORY_AUTO_EXTRACT` | `false` | Auto-extract `[MEMORY]:` tags from agent output |
| `MEMORY_DIR` | *(platform config dir)* | Custom path for memory files |

Default memory directory:

- **macOS:** `~/Library/Application Support/SlackClaw/memory/`
- **Linux:** `~/.config/SlackClaw/memory/`
- **Windows:** `%APPDATA%\SlackClaw\memory\`

## Scopes

Each memory belongs to one of three scopes:

| Scope | Key | Visibility |
|-------|-----|------------|
| **workspace** | `"workspace"` | All users and threads |
| **user** | Slack user ID (e.g. `U12345`) | Only that user's commands |
| **thread** | `<channel>:<thread_ts>` | Only within a specific Slack thread |

When an agent runs, memories from all three scopes (workspace + user + current thread) are searched and injected into the prompt.

## Storage

### Markdown Files

Each memory is a standalone `.md` file with YAML frontmatter:

```
~/.config/SlackClaw/memory/
  workspace/
    a1b2c3d4e5f6g7h8.md
  user/U12345/
    f9e8d7c6b5a4e3d2.md
  thread/C0123_1709234567-123456/
    1a2b3c4d5e6f7g8h.md
```

File format:

```markdown
---
id: a1b2c3d4e5f6g7h8
scope: workspace
category: note
source_agent: claude
source_task_id: abc123
created_at: 2026-03-04T10:30:00+00:00
---
Project uses Python 3.11 with pytest for testing
```

The memory ID is a 16-character SHA-256 prefix derived from `scope:scope_key:content`.

### SQLite FTS5 Index

Alongside the files, all memories are indexed in the `memories` table with a companion `memories_fts` FTS5 virtual table. This gives BM25-ranked full-text search with `porter unicode61` tokenization. The FTS index is kept in sync via SQLite triggers (insert/update/delete).

## Slack Commands

Type these in your command channel:

```
MEMORY store project uses Python 3.11        # store workspace-scoped note (default)
MEMORY store user:I prefer dark mode          # store user-scoped note
MEMORY store thread:this PR fixes auth        # store thread-scoped note
MEMORY recall deployment process              # full-text search across all scopes
MEMORY forget a1b2c3d4e5f6g7h8               # delete a specific memory by ID
MEMORY list                                   # list your recent memories
```

### store

Creates a new memory. Default scope is **workspace**. Use the `user:` or `thread:` prefix to choose a different scope.

Before storing, a Jaccard word-overlap check (threshold 0.85) runs against existing memories in the same scope to prevent near-duplicates.

### recall

Performs a BM25-ranked FTS5 search across your user, workspace, and current thread scopes. Returns up to 10 results.

### forget

Deletes a memory by its 16-character hex ID. Removes both the `.md` file and the SQLite index entry.

### list

Shows your 20 most recent memories across user, workspace, and thread scopes.

## Automatic Prompt Injection

When an AI agent command runs (CLAUDE, CODEX, KIMI), the memory system automatically:

1. **Extracts keywords** from the user's prompt (stopwords removed, up to 8 keywords)
2. **Searches FTS5** across user + workspace + thread scopes using `keyword1 OR keyword2 OR ...`
3. **Falls back to recent memories** if no FTS matches are found (e.g. for short or ambiguous prompts like "who are you?")
4. **Formats a labeled list** capped at `MEMORY_INJECTION_MAX_CHARS` characters:
   ```
   Relevant memories:
   - [workspace] Project uses Python 3.11 with pytest for testing
   - [user] Preferred assistant name: xiaolinke
   ```
5. **Injects into the agent prompt** as system context (see Agent Integration below)

## Auto-Extraction

When `MEMORY_AUTO_EXTRACT=true`, agents are instructed to tag durable facts in their output:

```
[MEMORY]: Project uses Python 3.11 with pytest for testing
[REMEMBER]: Deploy process requires git tag then push
```

After each successful agent run, SlackClaw scans the output for `[MEMORY]:` or `[REMEMBER]:` tags and stores each one automatically. These are scoped to the current thread if thread metadata is available, otherwise to the user scope.

## Thread Context

Separate from persistent memory, SlackClaw maintains per-thread conversation history in the `thread_context` SQLite table. When multiple agents run in the same Slack thread, they share this context.

Each agent run appends a record:

```
user=<prompt>
agent=<agent_name> response=<output_summary>
```

This context is injected into subsequent agent prompts within the same thread, enabling follow-up questions and iterative workflows.

Thread context is ephemeral -- it lives in SQLite only (no files) and is not searchable via MEMORY commands. It is visible on the dashboard under "Thread Contexts".

## Agent Integration

Each agent CLI receives memory differently:

### Claude Code

- **System context** (memories + thread context + format instructions): passed via `--append-system-prompt`
- **User prompt**: piped via stdin with `-p` flag
- This separation ensures Claude treats memories as system-level instructions rather than user input

### Codex

- **System context + user prompt**: combined and piped via stdin (using `-` as the prompt placeholder)
- Codex does not have a separate system prompt flag, so both are merged

### Kimi

- **System context + user prompt**: combined into a single `-p` argument
- Kimi uses session IDs (`-S`) for native thread continuity on top of the injected context

## Profile Hints

The memory system also extracts lightweight profile hints from stored memories and thread context. If memories or context contain patterns like "your name is X" or "my name is X", an assistant profile block is generated:

```
Assistant profile:
- Preferred assistant name: xiaolinke
- Preferred tone: encouraging and positive
```

This is included in the system context to help agents maintain persona consistency. For identity queries (e.g. "who are you?"), an additional identity response rule is injected.

## Retention and Cleanup

Memories that have not been accessed (searched or listed) within `MEMORY_RETENTION_DAYS` (default 90) are automatically purged on app startup. Both the SQLite entry and the `.md` file are removed.

The `access_count` and `last_accessed_at` fields are updated each time a memory appears in a search result or is otherwise touched.

## Dashboard

The local dashboard (`http://127.0.0.1:<port>`) shows:

- **Memories card** -- table with ID, scope, scope key, category, content, source agent, access count, and last updated time
- **Thread Contexts card** -- clickable rows that open a chat-style modal showing the full conversation history for each thread
- **Stats** -- memory count included in the stats summary

Both cards support pagination (10 items per page).
