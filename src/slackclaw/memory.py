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
    if scope == MemoryScope.USER:
        return f"user/{scope_key}"
    if scope == MemoryScope.THREAD:
        return f"thread/{scope_key}"
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
            args, trigger_user=trigger_user, store=store, base_dir=base_dir,
        )
    if sub == "recall":
        return _cmd_recall(
            args, trigger_user=trigger_user, channel_id=channel_id,
            thread_ts=thread_ts, store=store,
        )
    if sub == "forget":
        return _cmd_forget(args, store=store)
    if sub == "list":
        return _cmd_list(trigger_user=trigger_user, store=store)
    return "unknown memory subcommand", f"expected store|recall|forget|list, got: {sub}"


def _cmd_store(
    text: str,
    *,
    trigger_user: str,
    store: StateStore,
    base_dir: Path,
) -> tuple[str, str]:
    if not text.strip():
        return "memory store failed", "empty content"

    # workspace scope
    if text.lower().startswith("workspace:"):
        content = text[len("workspace:"):].strip()
        scope = MemoryScope.WORKSPACE
        scope_key = "workspace"
    else:
        content = text.strip()
        scope = MemoryScope.USER
        scope_key = trigger_user

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


def _cmd_list(*, trigger_user: str, store: StateStore) -> tuple[str, str]:
    results = store.list_memories(trigger_user, limit=20)
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
    keywords = extract_prompt_keywords(prompt_text)
    if not keywords:
        return ""

    scope_keys = [trigger_user, "workspace"]
    if channel_id and thread_ts:
        scope_keys.append(f"{channel_id}:{thread_ts}")

    query = " OR ".join(keywords)
    results = store.search_memories(scope_keys=scope_keys, query=query, limit=10)
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
