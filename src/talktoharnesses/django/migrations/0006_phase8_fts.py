"""Vendor-specific full-text indexes derived from the shared search document.

PostgreSQL gets a stored ``tsvector`` generated from ``normalized_text`` plus a
GIN index. SQLite gets an FTS5 external-content virtual table kept in sync by
triggers and backfilled with ``rebuild``. Both are private derived indexes:
``SearchDocument.normalized_text`` remains the only content source and
``application.search_documents`` the only knowledge source (``docs/phase8.md``
Work Package 4). Reversal drops only the derived structures.
"""

from __future__ import annotations

from django.db import migrations

_TABLE = "talktoharnesses_search_document"
_FTS = "talktoharnesses_search_document_fts"

POSTGRES_FORWARD = (
    f"ALTER TABLE {_TABLE} ADD COLUMN search_vector tsvector GENERATED ALWAYS AS "
    "(to_tsvector('simple', coalesce(normalized_text, ''))) STORED",
    f"CREATE INDEX tth_search_vector_gin ON {_TABLE} USING GIN (search_vector)",
)

POSTGRES_REVERSE = (
    "DROP INDEX IF EXISTS tth_search_vector_gin",
    f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS search_vector",
)

# CREATE VIRTUAL TABLE fails loudly when the deployed SQLite lacks FTS5; there
# is deliberately no fallback to the Phase 5 substring scan.
SQLITE_FORWARD = (
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

SQLITE_REVERSE = (
    f"DROP TRIGGER IF EXISTS {_FTS}_au",
    f"DROP TRIGGER IF EXISTS {_FTS}_ad",
    f"DROP TRIGGER IF EXISTS {_FTS}_ai",
    f"DROP TABLE IF EXISTS {_FTS}",
)


def _execute(schema_editor, statements):
    for statement in statements:
        schema_editor.execute(statement)


def forward(apps, schema_editor):
    postgres = schema_editor.connection.vendor == "postgresql"
    _execute(schema_editor, POSTGRES_FORWARD if postgres else SQLITE_FORWARD)


def reverse(apps, schema_editor):
    postgres = schema_editor.connection.vendor == "postgresql"
    _execute(schema_editor, POSTGRES_REVERSE if postgres else SQLITE_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("talktoharnesses", "0005_phase8_backfill"),
    ]

    operations = [
        # Derived indexes only: no model state changes, and SearchDocument rows
        # survive reversal.
        migrations.RunPython(forward, reverse),
    ]
