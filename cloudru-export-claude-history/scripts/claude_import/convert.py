"""Normalize full Claude exports into native message trees and scoped references."""

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, JsonValue

from claude_import.knowledge import (
    References,
    attachment_reference,
    category,
    identity,
    import_knowledge,
    object_json,
)
from claude_import.models import InputFile, Plan, PreparedChat, encode, private_json
from claude_import.source import json_records, tree_digest

ROOTS: set[str | None] = {
    None,
    "",
    "00000000-0000-0000-0000-000000000000",
    "00000000-0000-4000-8000-000000000000",
}
MAX_CHUNK_BYTES: int = 3 * 1024 * 1024


class ChatMessage(BaseModel):
    """Source fields required for role, date and tree preservation."""

    uuid: str = Field(min_length=1)
    sender: Literal["human", "user", "assistant"]
    text: str = ""
    content: list[dict[str, JsonValue]] = Field(default_factory=list)
    created_at: AwareDatetime
    updated_at: AwareDatetime | None = None
    parent_message_uuid: str | None = None
    attachments: list[dict[str, JsonValue]] = Field(default_factory=list)
    files: list[dict[str, JsonValue]] = Field(default_factory=list)


class Conversation(BaseModel):
    """A normal account-export conversation, including empty conversations."""

    uuid: str = Field(min_length=1)
    name: str = ""
    summary: str = ""
    created_at: AwareDatetime
    updated_at: AwareDatetime | None = None
    chat_messages: list[ChatMessage]


def milliseconds(value: datetime) -> int:
    """Preserve an aware source date as Unix milliseconds."""
    return int(value.timestamp() * 1000)


def source_timestamp(value: JsonValue) -> int:
    """Reject source dates that omit their timezone."""
    if not isinstance(value, str):
        raise ValueError("Source timestamp must be an ISO string")
    normalized: str = re.sub(
        r"(\d{2}:\d{2}:\d{2})[.,](\d+)",
        lambda match: match[1] + "." + (match[2] + "000000")[:6],
        value.replace("Z", "+00:00"),
    )
    parsed: datetime = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("Source timestamp must include its timezone")
    return milliseconds(parsed)


def fingerprint(item: dict[str, JsonValue]) -> str:
    """Select exact message facts shared with the native history verifier."""
    message: JsonValue = item["message"]
    if not isinstance(message, dict):
        raise ValueError("Expected a prepared message")
    return encode(
        {
            "id": item["eventId"],
            "parentId": item["parentId"],
            "role": message["role"],
            "content": message["content"],
            "timestamp": item["timestampMs"],
        }
    )


def normalize(
    work: Path, batch: str, agent: str, archives: int, inputs: list[InputFile]
) -> Plan:
    """Build bounded import batches while preserving all parent edges on disk."""
    source: Path = work / "source"
    prepared: Path = work / "prepared"
    prepared.mkdir(mode=0o700)
    references: References = References(work / "references", batch)
    statistics: dict[str, int] = import_knowledge(source, references)
    statistics.update(
        {
            "conversations": 0,
            "design_chats": 0,
            "messages": 0,
            "empty_chats": 0,
            "branch_chats": 0,
            "branch_points": 0,
            "extracted_attachments": 0,
            "file_references": 0,
            "service_blocks_archived": 0,
        }
    )
    notices: list[str] = [
        "Исходная выбранная ветка в экспорте не указана: активной станет ветка с самым поздним конечным сообщением.",
        "Обычные чаты не содержат связи с проектами; такие связи не выдумываются.",
        "Файлы, представленные только именем и идентификатором, нельзя восстановить как бинарные вложения.",
        "Проекты представлены справочными папками и группами чатов, без создания фиктивных Git-репозиториев.",
    ]
    spool: Path = work / "merge.sqlite"
    with closing(sqlite3.connect(spool)) as database, database:
        database.execute(
            "CREATE TABLE chats (id TEXT PRIMARY KEY, kind TEXT, title TEXT, updated INTEGER, created INTEGER)"
        )
        database.execute(
            "CREATE TABLE messages (chat TEXT, id TEXT, parent TEXT, created INTEGER, updated INTEGER, payload TEXT, PRIMARY KEY(chat,id))"
        )
        for path in sorted(source.rglob("*.json")):
            group: str = category(path, source)
            if group == "conversations":
                for raw in json_records(path):
                    chat: Conversation = Conversation.model_validate(raw)
                    key: str = "conversation/" + chat.uuid
                    updated: int = milliseconds(chat.updated_at or chat.created_at)
                    database.execute(
                        "INSERT INTO chats VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,updated=excluded.updated,created=excluded.created WHERE excluded.updated>=chats.updated",
                        (
                            key,
                            "conversation",
                            chat.name,
                            updated,
                            milliseconds(chat.created_at),
                        ),
                    )
                    if chat.summary:
                        references.add(
                            "summaries",
                            key,
                            "Историческая сводка: " + chat.name,
                            chat.summary,
                        )
                    for message in chat.chat_messages:
                        links: list[str] = []
                        for attachment in message.attachments:
                            linked: str | None = attachment_reference(
                                references, attachment
                            )
                            if linked:
                                links.append(linked)
                                statistics["extracted_attachments"] += 1
                        statistics["file_references"] += len(message.files)
                        statistics["service_blocks_archived"] += sum(
                            block.get("type") != "text" for block in message.content
                        )
                        text: str = message.text or "".join(
                            str(block["text"])
                            for block in message.content
                            if block.get("type") == "text"
                            and isinstance(block.get("text"), str)
                        )
                        if links:
                            text += "\n\n[Извлечённые тексты вложений]\n" + "\n".join(
                                links
                            )
                        if message.files:
                            text += (
                                "\n\n[Выгрузка содержит ссылки на файлы без бинарных оригиналов]\n"
                                + "\n".join(
                                    str(
                                        item.get("file_name")
                                        or item.get("file_uuid")
                                        or "Файл"
                                    )
                                    for item in message.files
                                )
                            )
                        if not text:
                            text = "[Служебная запись Claude. Полные исходные данные сохранены отдельно.]"
                        payload: dict[str, JsonValue] = {
                            "role": "assistant"
                            if message.sender == "assistant"
                            else "user",
                            "content": [{"type": "text", "text": text}],
                        }
                        database.execute(
                            "INSERT INTO messages VALUES(?,?,?,?,?,?) ON CONFLICT(chat,id) DO UPDATE SET parent=excluded.parent,created=excluded.created,updated=excluded.updated,payload=excluded.payload WHERE excluded.updated>=messages.updated",
                            (
                                key,
                                message.uuid,
                                message.parent_message_uuid,
                                milliseconds(message.created_at),
                                milliseconds(message.updated_at or message.created_at),
                                encode(payload),
                            ),
                        )
            elif group == "design_chats":
                design: dict[str, JsonValue] = object_json(path)
                key = "design/" + str(design["uuid"])
                messages: JsonValue = design.get("messages")
                if not isinstance(messages, list):
                    raise ValueError("Unsupported design chat messages")
                updated = source_timestamp(design["updated_at"])
                database.execute(
                    "INSERT INTO chats VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,updated=excluded.updated,created=excluded.created WHERE excluded.updated>=chats.updated",
                    (
                        key,
                        "design",
                        str(design.get("title") or "Claude Design"),
                        updated,
                        source_timestamp(design["created_at"]),
                    ),
                )
                previous: str | None = None
                for raw in messages:
                    if not isinstance(raw, dict) or not isinstance(
                        raw.get("content"), dict
                    ):
                        raise ValueError("Unsupported design message")
                    content: dict[str, JsonValue] = raw["content"]
                    if not isinstance(content.get("content", ""), str):
                        raise ValueError("Design message text must be a string")
                    role: str = str(raw.get("role"))
                    if role not in {"human", "user", "assistant"}:
                        raise ValueError("Unsupported design role")
                    created: int = source_timestamp(raw["created_at"])
                    text = str(content.get("content") or "")
                    attachments: JsonValue = content.get("attachments", [])
                    if not isinstance(attachments, list):
                        raise ValueError("Unsupported design attachments")
                    for index, attachment in enumerate(attachments):
                        if not isinstance(attachment, dict) or not isinstance(
                            attachment.get("content"), str
                        ):
                            raise ValueError("Unsupported design attachment")
                        linked = references.add(
                            "design-attachments",
                            key + "/" + str(raw["uuid"]) + "/" + str(index),
                            str(attachment.get("name") or "Вложение Claude Design"),
                            attachment["content"],
                        )
                        text += (
                            "\n\n[Вложение источника, не устанавливается как навык]\n"
                            + linked
                        )
                    payload = {
                        "role": "assistant" if role == "assistant" else "user",
                        "content": [
                            {
                                "type": "text",
                                "text": text
                                or "[Служебная запись Claude Design; исходные данные сохранены отдельно.]",
                            }
                        ],
                    }
                    database.execute(
                        "INSERT INTO messages VALUES(?,?,?,?,?,?) ON CONFLICT(chat,id) DO UPDATE SET parent=excluded.parent,created=excluded.created,updated=excluded.updated,payload=excluded.payload WHERE excluded.updated>=messages.updated",
                        (
                            key,
                            str(raw["uuid"]),
                            previous,
                            created,
                            max(updated, created),
                            encode(payload),
                        ),
                    )
                    previous = str(raw["uuid"])
            database.commit()
        chats: list[PreparedChat] = []
        for source_id, kind, title, _updated, chat_created in database.execute(
            "SELECT id,kind,title,updated,created FROM chats ORDER BY id"
        ):
            session_key: str = f"agent:{agent}:claude-import:{identity(source_id)}"
            rows: dict[str, tuple[str | None, int, str]] = {
                identifier: (parent, created, serialized)
                for identifier, parent, created, serialized in database.execute(
                    "SELECT id,parent,created,payload FROM messages WHERE chat=? ORDER BY created,rowid",
                    (source_id,),
                )
            }
            children: dict[str | None, list[str]] = defaultdict(list)
            for identifier, (parent, _created, _serialized) in rows.items():
                if parent not in rows and parent not in ROOTS:
                    raise ValueError(
                        "A source parent message is missing from its conversation"
                    )
                children[parent if parent in rows else None].append(identifier)
            order: list[str] = []
            stack: list[str] = list(reversed(children[None]))
            while stack:
                identifier = stack.pop()
                order.append(identifier)
                stack.extend(reversed(children.get(identifier, [])))
            if len(order) != len(rows):
                raise ValueError("Cycle in source message parents")
            leaves: list[str] = [
                identifier for identifier in rows if not children.get(identifier)
            ]
            active: str | None = (
                max(leaves, key=lambda identifier: (rows[identifier][1], identifier))
                if leaves
                else None
            )
            active_path: set[str] = set()
            cursor: str | None = active
            while cursor in rows:
                active_path.add(cursor)
                cursor = rows[cursor][0]
            for values in children.values():
                values.sort(key=lambda identifier: identifier in active_path)
            order = []
            stack = list(reversed(children[None]))
            while stack:
                identifier = stack.pop()
                order.append(identifier)
                stack.extend(reversed(children.get(identifier, [])))
            branch_points: int = sum(
                len(values) > 1 for key, values in children.items() if key is not None
            )
            statistics["branch_points"] += branch_points
            statistics["branch_chats"] += bool(branch_points)
            statistics["empty_chats"] += not rows
            statistics[
                "conversations" if kind == "conversation" else "design_chats"
            ] += 1
            statistics["messages"] += len(rows)
            directory: Path = prepared / "chats" / identity(source_id)
            directory.mkdir(parents=True, mode=0o700)
            chunks: list[str] = []
            batch_items: list[dict[str, JsonValue]] = []
            byte_count: int = 0
            fingerprints: dict[str, str] = {}

            def flush() -> None:
                """Publish one bounded RPC input file."""
                nonlocal batch_items, byte_count
                if not batch_items:
                    return
                filename: Path = directory / f"part-{len(chunks):05d}.json"
                private_json(
                    filename, {"sessionKey": session_key, "messages": batch_items}
                )
                chunks.append(str(filename.relative_to(prepared)))
                batch_items = []
                byte_count = 0

            for identifier in order:
                parent, created, serialized = rows[identifier]
                native_id: str = identity(source_id + "/" + identifier)
                item: dict[str, JsonValue] = {
                    "eventId": native_id,
                    "parentId": identity(source_id + "/" + str(parent))
                    if parent in rows
                    else None,
                    "timestampMs": created,
                    "idempotencyKey": "claude-export:" + source_id + "/" + identifier,
                    "message": json.loads(serialized),
                }
                size: int = len(encode(item).encode())
                if size > MAX_CHUNK_BYTES:
                    raise ValueError(
                        "One visible message exceeds the 3 MiB import limit; no text was clipped"
                    )
                if batch_items and (
                    len(batch_items) >= 100 or byte_count + size > MAX_CHUNK_BYTES
                ):
                    flush()
                batch_items.append(item)
                byte_count += size
                fingerprints[native_id] = fingerprint(item)
            flush()
            digest: str = hashlib.sha256(
                "".join(
                    fingerprints[key] + "\n" for key in sorted(fingerprints)
                ).encode()
            ).hexdigest()
            chat_plan: PreparedChat = PreparedChat(
                source_id=source_id,
                kind=kind,
                session_key=session_key,
                title=title or "Claude",
                category="Claude Design" if kind == "design" else "Claude — импорт",
                messages=len(rows),
                digest=digest,
                chunks=chunks,
                leaves=[identity(source_id + "/" + value) for value in leaves],
                active_leaf=identity(source_id + "/" + active) if active else None,
                created_ms=chat_created,
            )
            private_json(
                directory / "chat.json",
                chat_plan.model_dump(mode="json"),
            )
            chats.append(chat_plan)
    groups: set[str] = {
        category(path, source) for path in source.rglob("*") if path.is_file()
    }
    unknown: set[str] = groups - {
        "conversations",
        "projects",
        "memories",
        "frames",
        "design_chats",
        "feedback",
        "light_metadata",
    }
    if unknown:
        notices.append(
            "Категории без нативного преобразования, сохранённые в исходниках: "
            + ", ".join(sorted(unknown))
        )
    references.index()
    plan: Plan = Plan(
        batch_id=batch,
        agent=agent,
        archives=archives,
        inputs=inputs,
        originals_digest=tree_digest(work / "originals"),
        source_digest=tree_digest(source),
        prepared_digest=tree_digest(prepared),
        reference_digest=tree_digest(references.directory),
        chats=chats,
        references=list(references.items.values()),
        statistics=statistics,
        notices=notices,
    )
    private_json(work / "plan.json", plan.model_dump(mode="json"))
    return plan
