"""Phase 11 search derived columns, retention policy, and exemption flag.

Adds ``search_title`` / ``search_body`` / ``snippet_text`` on search documents,
owner-scoped retention policy rows, and ``retention_exempt`` on conversation
aggregates (column + JSON). Search columns are backfilled from retained
projection rows using a migration-local builder copy required for Django
migration stability. Reverse drops only Phase 11 fields/rows.
"""

from __future__ import annotations

import json
import re

from django.db import migrations, models


def _normalize_terms(text: str) -> tuple[str, ...]:
    folded = text.casefold()
    stripped = "".join(char if char.isalnum() else " " for char in folded)
    return tuple(stripped.split())


def _normalize(text: str) -> str:
    return " ".join(_normalize_terms(text))


def _normalize_arguments(arguments: dict) -> str:
    try:
        return json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(arguments)


def _build_fields(
    *,
    title: str,
    message_texts: list[str],
    tool_names: list[str],
    tool_arguments: list[dict],
    tool_paths: list[str],
    tool_output_tails: list[str],
) -> tuple[str, str, str, str]:
    """Historical migration copy of the Phase 11 search-document builder."""
    search_title = _normalize(title) if title else ""
    display: list[str] = []
    for text in message_texts:
        if text:
            display.append(text)
    for name in tool_names:
        if name:
            display.append(name)
    for args in tool_arguments:
        if args:
            display.append(_normalize_arguments(args))
    for path in tool_paths:
        if path:
            display.append(path)
    for tail in tool_output_tails:
        if tail:
            display.append(tail)
    snippet_text = " ".join(p for p in display if p)
    search_body = _normalize(snippet_text) if snippet_text else ""
    if search_title and search_body:
        normalized_text = f"{search_title} {search_body}"
    else:
        normalized_text = search_title or search_body
    return search_title, search_body, snippet_text, normalized_text


def _display_title(conversation: dict) -> str:
    if conversation.get("title_native"):
        return str(conversation["title_native"])
    if conversation.get("title_manual"):
        return str(conversation["title_manual"])
    if conversation.get("title_derived"):
        return str(conversation["title_derived"])
    return "Untitled conversation"


def _backfill_phase11(apps, schema_editor) -> None:
    ConversationAggregate = apps.get_model("talktoharnesses", "ConversationAggregate")
    MessageRecord = apps.get_model("talktoharnesses", "MessageRecord")
    SearchDocument = apps.get_model("talktoharnesses", "SearchDocument")
    ToolRecord = apps.get_model("talktoharnesses", "ToolRecord")
    del schema_editor

    for aggregate in ConversationAggregate.objects.iterator():
        state = dict(aggregate.state or {})
        conversation = dict(state.get("conversation") or {})
        if "retention_exempt" not in conversation:
            conversation["retention_exempt"] = False
            state["conversation"] = conversation
            aggregate.state = state
            aggregate.retention_exempt = False
            aggregate.save(update_fields=("state", "retention_exempt"))
        else:
            aggregate.retention_exempt = bool(conversation.get("retention_exempt"))
            aggregate.save(update_fields=("retention_exempt",))

        cid = aggregate.conversation_id
        message_texts = list(
            MessageRecord.objects.filter(conversation_id=cid).values_list("text", flat=True)
        )
        tools = list(ToolRecord.objects.filter(conversation_id=cid))
        search_title, search_body, snippet_text, normalized_text = _build_fields(
            title=_display_title(conversation) if conversation else aggregate.title,
            message_texts=[str(t) for t in message_texts if t],
            tool_names=[str(t.tool_name) for t in tools],
            tool_arguments=[dict(t.arguments or {}) for t in tools],
            tool_paths=[str(p) for t in tools for p in (t.paths or [])],
            tool_output_tails=[str(t.output_tail) for t in tools if t.output_tail],
        )
        SearchDocument.objects.update_or_create(
            conversation_id=cid,
            defaults={
                "owner_id": aggregate.owner_id,
                "normalized_text": normalized_text,
                "search_title": search_title,
                "search_body": search_body,
                "snippet_text": snippet_text,
                "updated_at": aggregate.updated_at,
            },
        )


def _noop_reverse(apps, schema_editor) -> None:
    # Reverse only drops schema; pruned history cannot be reconstructed.
    del apps, schema_editor


_TABLE = "talktoharnesses_search_document"
_FTS = "talktoharnesses_search_document_fts"

_SQLITE_DROP_FTS = (
    f"DROP TRIGGER IF EXISTS {_FTS}_au",
    f"DROP TRIGGER IF EXISTS {_FTS}_ad",
    f"DROP TRIGGER IF EXISTS {_FTS}_ai",
    f"DROP TABLE IF EXISTS {_FTS}",
)

_SQLITE_CREATE_FTS = (
    f"CREATE VIRTUAL TABLE {_FTS} USING fts5("
    f"conversation_id UNINDEXED, normalized_text, content='{_TABLE}', "
    "tokenize='unicode61 remove_diacritics 0')",
    f"""CREATE TRIGGER {_FTS}_ai AFTER INSERT ON {_TABLE} BEGIN
        INSERT INTO {_FTS}(rowid, conversation_id, normalized_text)
        VALUES (new.rowid, new.conversation_id, new.normalized_text);
    END""",
    f"""CREATE TRIGGER {_FTS}_ad AFTER DELETE ON {_TABLE} BEGIN
        INSERT INTO {_FTS}({_FTS}, rowid, conversation_id, normalized_text)
        VALUES ('delete', old.rowid, old.conversation_id, old.normalized_text);
    END""",
    f"""CREATE TRIGGER {_FTS}_au AFTER UPDATE ON {_TABLE} BEGIN
        INSERT INTO {_FTS}({_FTS}, rowid, conversation_id, normalized_text)
        VALUES ('delete', old.rowid, old.conversation_id, old.normalized_text);
        INSERT INTO {_FTS}(rowid, conversation_id, normalized_text)
        VALUES (new.rowid, new.conversation_id, new.normalized_text);
    END""",
    f"INSERT INTO {_FTS}({_FTS}) VALUES('rebuild')",
)


def _drop_sqlite_fts(apps, schema_editor) -> None:
    del apps
    if schema_editor.connection.vendor != "sqlite":
        return
    for statement in _SQLITE_DROP_FTS:
        schema_editor.execute(statement)


def _recreate_sqlite_fts(apps, schema_editor) -> None:
    del apps
    if schema_editor.connection.vendor != "sqlite":
        return
    for statement in _SQLITE_CREATE_FTS:
        schema_editor.execute(statement)


class Migration(migrations.Migration):
    dependencies = [
        ("talktoharnesses", "0007_phase9_recovery"),
    ]

    operations = [
        # SQLite table rewrites for AddField drop FTS triggers; tear down first.
        migrations.RunPython(_drop_sqlite_fts, _recreate_sqlite_fts),
        migrations.AddField(
            model_name="conversationaggregate",
            name="retention_exempt",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="searchdocument",
            name="search_title",
            field=models.TextField(default=""),
        ),
        migrations.AddField(
            model_name="searchdocument",
            name="search_body",
            field=models.TextField(default=""),
        ),
        migrations.AddField(
            model_name="searchdocument",
            name="snippet_text",
            field=models.TextField(default=""),
        ),
        migrations.CreateModel(
            name="RetentionPolicyRecord",
            fields=[
                (
                    "owner_id",
                    models.CharField(
                        editable=False, max_length=255, primary_key=True, serialize=False
                    ),
                ),
                ("months", models.PositiveSmallIntegerField()),
                ("updated_at", models.DateTimeField()),
            ],
            options={
                "db_table": "talktoharnesses_retention_policy",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("months__gte", 1), ("months__lte", 120)),
                        name="tth_retention_months_range",
                    ),
                ],
            },
        ),
        migrations.RunPython(_backfill_phase11, _noop_reverse),
        migrations.RunPython(_recreate_sqlite_fts, _drop_sqlite_fts),
    ]


# Keep unused import noise down for migration checkers that scan the module.
_ = re
