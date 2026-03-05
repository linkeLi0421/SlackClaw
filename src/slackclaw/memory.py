from __future__ import annotations

import hashlib
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from .models import MemoryCategory, MemoryRecord, MemoryScope
from .state_store import StateStore


def _build_memory_id(scope: str, scope_key: str, content: str) -> str:
    raw = f"{scope}:{scope_key}:{content}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _memory_dir(memory_dir_override: str = "") -> Path:
    if memory_dir_override:
        return Path(memory_dir_override)

    app_name = "SlackClaw"
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    return base / app_name / "memory"


def _write_memory_file(file_path: Path, record: MemoryRecord) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"id: {record.memory_id}\n"
        f"scope: {record.scope.value}\n"
        f"category: {record.category.value}\n"
        f"source_agent: {record.source_agent}\n"
        f"source_task_id: {record.source_task_id}\n"
        f"created_at: {record.created_at}\n"
        "---\n"
    )
    file_path.write_text(frontmatter + record.content, encoding="utf-8")


def _read_memory_file(file_path: Path) -> dict[str, str]:
    text = file_path.read_text(encoding="utf-8")
    result: dict[str, str] = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    result[key.strip()] = val.strip()
            result["content"] = text[end + 5:].strip()
        else:
            result["content"] = text
    else:
        result["content"] = text
    return result


def _delete_memory_file(file_path: Path) -> None:
    if file_path.is_file():
        file_path.unlink()


def _scope_subdir(scope: MemoryScope, scope_key: str) -> str:
    safe_scope_key = re.sub(r"[^A-Za-z0-9._-]+", "_", scope_key).strip("._") or "default"
    if scope == MemoryScope.USER:
        return f"user/{safe_scope_key}"
    if scope == MemoryScope.THREAD:
        return f"thread/{safe_scope_key}"
    return "workspace"


def _is_similar(existing: str, new: str, threshold: float = 0.85) -> bool:
    words_a = set(existing.lower().split())
    words_b = set(new.lower().split())
    if not words_a and not words_b:
        return True
    if not words_a or not words_b:
        return False
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) >= threshold


def handle_memory_command(
    subcommand: str,
    args: str,
    trigger_user: str,
    channel_id: str,
    thread_ts: str,
    store: StateStore,
    memory_dir_path: str = "",
) -> tuple[str, str]:
    """Route memory sub-commands.  Returns (summary, details)."""

    base_dir = _memory_dir(memory_dir_path)
    sub = subcommand.lower().strip()

    if sub == "store":
        return _cmd_store(
            args,
            trigger_user=trigger_user,
            channel_id=channel_id,
            thread_ts=thread_ts,
            store=store,
            base_dir=base_dir,
        )
    if sub == "recall":
        return _cmd_recall(
            args, trigger_user=trigger_user, channel_id=channel_id,
            thread_ts=thread_ts, store=store,
        )
    if sub == "forget":
        return _cmd_forget(args, store=store)
    if sub == "list":
        return _cmd_list(
            trigger_user=trigger_user,
            channel_id=channel_id,
            thread_ts=thread_ts,
            store=store,
        )
    return "unknown memory subcommand", f"expected store|recall|forget|list, got: {sub}"


def _cmd_store(
    text: str,
    *,
    trigger_user: str,
    channel_id: str,
    thread_ts: str,
    store: StateStore,
    base_dir: Path,
) -> tuple[str, str]:
    if not text.strip():
        return "memory store failed", "empty content"

    raw = text.strip()
    lowered = raw.lower()

    # Default manual stores to workspace scope.
    scope = MemoryScope.WORKSPACE
    scope_key = "workspace"
    content = raw

    if lowered.startswith("workspace:"):
        content = raw[len("workspace:"):].strip()
        scope = MemoryScope.WORKSPACE
        scope_key = "workspace"
    elif lowered.startswith("user:"):
        content = raw[len("user:"):].strip()
        scope = MemoryScope.USER
        scope_key = trigger_user
    elif lowered.startswith("thread:"):
        content = raw[len("thread:"):].strip()
        scope = MemoryScope.THREAD
        scope_key = f"{channel_id}:{thread_ts}"

    if not content:
        return "memory store failed", "empty content after scope prefix"

    # Dedup check
    existing = store.list_memories(scope_key, limit=200)
    for mem in existing:
        if _is_similar(mem.content, content):
            return "memory already exists (duplicate)", f"similar to memory {mem.memory_id}: {mem.content[:80]}"

    memory_id = _build_memory_id(scope.value, scope_key, content)
    now = datetime.now(UTC).isoformat()
    subdir = _scope_subdir(scope, scope_key)
    file_path = base_dir / subdir / f"{memory_id}.md"

    record = MemoryRecord(
        memory_id=memory_id,
        scope=scope,
        scope_key=scope_key,
        category=MemoryCategory.NOTE,
        content=content,
        file_path=str(file_path),
        created_at=now,
        updated_at=now,
        last_accessed_at=now,
    )
    _write_memory_file(file_path, record)
    store.upsert_memory(record)
    return "memory stored", f"id={memory_id} scope={scope.value} content={content[:100]}"


def _cmd_recall(
    query: str,
    *,
    trigger_user: str,
    channel_id: str,
    thread_ts: str,
    store: StateStore,
) -> tuple[str, str]:
    if not query.strip():
        return "memory recall failed", "empty query"
    scope_keys = [trigger_user, "workspace"]
    if channel_id and thread_ts:
        scope_keys.append(f"{channel_id}:{thread_ts}")
    results = store.search_memories(scope_keys=scope_keys, query=query, limit=10)
    if not results:
        return "no memories found", f"query: {query}"
    for r in results:
        store.touch_memory_access(r.memory_id)
    lines = [f"- [{r.memory_id}] {r.content[:120]}" for r in results]
    return f"found {len(results)} memories", "\n".join(lines)


def _cmd_forget(memory_id: str, *, store: StateStore) -> tuple[str, str]:
    memory_id = memory_id.strip()
    if not memory_id:
        return "memory forget failed", "no memory_id provided"
    record = store.get_memory(memory_id)
    if record is None:
        return "memory not found", f"id={memory_id}"
    _delete_memory_file(Path(record.file_path))
    store.delete_memory(memory_id)
    return "memory deleted", f"id={memory_id} content={record.content[:80]}"


def _cmd_list(
    *,
    trigger_user: str,
    channel_id: str,
    thread_ts: str,
    store: StateStore,
) -> tuple[str, str]:
    scope_keys = [f"{trigger_user}", "workspace"]
    if channel_id and thread_ts:
        scope_keys.append(f"{channel_id}:{thread_ts}")
    # Also include thread-local scope entries if they share the common C:T key format.
    # list_memories is keyed by scope_key only, so we include all known keys and de-dup by id.
    results: list[MemoryRecord] = []
    seen: set[str] = set()
    for key in scope_keys:
        for rec in store.list_memories(key, limit=20):
            if rec.memory_id in seen:
                continue
            seen.add(rec.memory_id)
            results.append(rec)
    results.sort(key=lambda r: r.updated_at, reverse=True)
    results = results[:20]
    if not results:
        return "no memories found", "you have no stored memories"
    lines = [f"- [{r.memory_id}] ({r.category.value}) {r.content[:120]}" for r in results]
    return f"{len(results)} memories", "\n".join(lines)


# --- Phase 2: Prompt injection helpers ---

_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would "
    "shall should may might can could of in to for on with at by from as into "
    "through during before after above below between out off over under again "
    "further then once here there when where why how all each every both few "
    "more most other some such no nor not only own same so than too very i me "
    "my myself we our ours ourselves you your yours yourself yourselves he him "
    "his himself she her hers herself it its itself they them their theirs "
    "themselves what which who whom this that these those am".split()
)


def extract_prompt_keywords(text: str, max_keywords: int = 8) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for w in words:
        if len(w) < 2 or w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        keywords.append(w)
        if len(keywords) >= max_keywords:
            break
    return keywords


def build_memory_context(
    store: StateStore,
    trigger_user: str,
    channel_id: str,
    thread_ts: str,
    prompt_text: str,
    max_chars: int = 2000,
) -> str:
    if not prompt_text.strip():
        return ""

    scope_keys = [trigger_user, "workspace"]
    if channel_id and thread_ts:
        scope_keys.append(f"{channel_id}:{thread_ts}")
    keywords = extract_prompt_keywords(prompt_text)
    results: list[MemoryRecord] = []
    if keywords:
        query = " OR ".join(keywords)
        results = store.search_memories(scope_keys=scope_keys, query=query, limit=10)
    if not results:
        # Fallback: for short/ambiguous prompts (e.g. "who are you?"), inject a few
        # recent memories so the agent can still preserve identity/preferences.
        results = _recent_memories_for_scopes(
            store,
            scope_keys=_preferred_scope_order(scope_keys, channel_id=channel_id, thread_ts=thread_ts),
            limit_total=6,
            limit_per_scope=3,
        )
    if not results:
        return ""

    for r in results:
        store.touch_memory_access(r.memory_id)

    lines: list[str] = []
    budget = max_chars
    for r in results:
        line = f"- [{r.scope.value}] {r.content}"
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line) + 1

    if not lines:
        return ""
    return "Relevant memories:\n" + "\n".join(lines)


def _preferred_scope_order(scope_keys: list[str], *, channel_id: str, thread_ts: str) -> list[str]:
    ordered: list[str] = []
    if channel_id and thread_ts:
        ordered.append(f"{channel_id}:{thread_ts}")
    ordered.append("workspace")
    for key in scope_keys:
        if key not in ordered:
            ordered.append(key)
    return ordered


def _recent_memories_for_scopes(
    store: StateStore,
    *,
    scope_keys: list[str],
    limit_total: int,
    limit_per_scope: int,
) -> list[MemoryRecord]:
    merged: list[MemoryRecord] = []
    seen: set[str] = set()
    for key in scope_keys:
        for rec in store.list_memories(key, limit=limit_per_scope):
            if rec.memory_id in seen:
                continue
            seen.add(rec.memory_id)
            merged.append(rec)
    merged.sort(key=lambda r: r.updated_at, reverse=True)
    return merged[:limit_total]


# --- Phase 3: Auto-extraction ---

_MEMORY_TAG_RE = re.compile(
    r"\[(?:MEMORY|REMEMBER)\]:\s*(.+?)(?:\n|$)", re.IGNORECASE
)


def extract_and_store_memories(
    store: StateStore,
    result_text: str,
    agent: str,
    trigger_user: str,
    task_id: str,
    channel_id: str = "",
    thread_ts: str = "",
    memory_dir_path: str = "",
) -> list[str]:
    """Scan result_text for [MEMORY]: or [REMEMBER]: tags and store them.

    Returns list of stored memory IDs.
    """
    base_dir = _memory_dir(memory_dir_path)
    matches = _MEMORY_TAG_RE.findall(result_text)
    stored_ids: list[str] = []

    for content in matches:
        content = content.strip()
        if not content:
            continue
        if channel_id and thread_ts:
            scope = MemoryScope.THREAD
            scope_key = f"{channel_id}:{thread_ts}"
        else:
            # Backward-compatible fallback if thread metadata is unavailable.
            scope = MemoryScope.USER
            scope_key = trigger_user
        memory_id = _build_memory_id(scope.value, scope_key, content)
        now = datetime.now(UTC).isoformat()
        subdir = _scope_subdir(scope, scope_key)
        file_path = base_dir / subdir / f"{memory_id}.md"

        # Dedup
        existing = store.get_memory(memory_id)
        if existing is not None:
            continue

        record = MemoryRecord(
            memory_id=memory_id,
            scope=scope,
            scope_key=scope_key,
            category=MemoryCategory.NOTE,
            content=content,
            file_path=str(file_path),
            source_task_id=task_id,
            source_agent=agent,
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
        )
        _write_memory_file(file_path, record)
        store.upsert_memory(record)
        stored_ids.append(memory_id)

    return stored_ids


# Patterns that indicate the user's input itself is a fact/preference worth storing.
_USER_INPUT_MEMORY_PATTERNS = [
    re.compile(
        r"(?:your\s+name\s+is|call\s+(?:yourself|you)\s+|you\s+are\s+called)\s+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:always|never|from\s+now\s+on|remember\s+(?:that|to)|don'?t\s+forget)\s+",
        re.IGNORECASE,
    ),
]


def extract_user_input_memories(
    store: StateStore,
    user_input: str,
    *,
    trigger_user: str,
    channel_id: str = "",
    thread_ts: str = "",
    task_id: str = "",
    memory_dir_path: str = "",
) -> list[str]:
    """Auto-store user input when it matches preference/identity patterns.

    Returns list of stored memory IDs.
    """
    if not user_input or not user_input.strip():
        return []

    text = user_input.strip()
    matched = any(pat.search(text) for pat in _USER_INPUT_MEMORY_PATTERNS)
    if not matched:
        return []

    base_dir = _memory_dir(memory_dir_path)
    scope = MemoryScope.USER
    scope_key = trigger_user
    memory_id = _build_memory_id(scope.value, scope_key, text)

    existing = store.get_memory(memory_id)
    if existing is not None:
        return []

    # Dedup by similarity against existing user memories
    for rec in store.list_memories(scope_key, limit=50):
        if _is_similar(rec.content, text):
            return []

    now = datetime.now(UTC).isoformat()
    subdir = _scope_subdir(scope, scope_key)
    file_path = base_dir / subdir / f"{memory_id}.md"

    record = MemoryRecord(
        memory_id=memory_id,
        scope=scope,
        scope_key=scope_key,
        category=MemoryCategory.PREFERENCE,
        content=text,
        file_path=str(file_path),
        source_task_id=task_id,
        source_agent="user",
        created_at=now,
        updated_at=now,
        last_accessed_at=now,
    )
    _write_memory_file(file_path, record)
    store.upsert_memory(record)
    return [memory_id]
