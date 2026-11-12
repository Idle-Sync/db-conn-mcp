"""Shared parameter types and behaviour hints for the MCP tools.

Every tool argument carries a schema ``description`` so a client sees what to pass
without reading the tool's docstring, and every tool declares MCP ``ToolAnnotations``
(read-only, destructive, idempotent, open-world) so a client can tell a harmless
lookup from a write before calling it. Parameters that several tools share are
defined once here; parameters unique to one tool stay next to that tool in
``server.py``.
"""

from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

# ---- Shared parameter types ----------------------------------------------------

DatabaseName = Annotated[
    str,
    Field(
        description=(
            "Name of a configured database, exactly as list_databases returns it "
            "(case-sensitive). Not a DSN, host or server-side database name. An unknown "
            "name is an error that lists the configured names."
        )
    ),
]

TableName = Annotated[
    str,
    Field(
        description=(
            "Table or view name exactly as list_tables shows it (case-sensitive, no "
            "quotes), optionally schema-qualified: 'orders' or 'public.orders'."
        )
    ),
]

ReadSql = Annotated[
    str,
    Field(
        description=(
            "One read-only SQL statement (SELECT, WITH, VALUES, ...). Put values in "
            "$1, $2, ... placeholders and pass them in `params`. Multiple statements "
            "are rejected."
        )
    ),
]

SqlParams = Annotated[
    list[Any] | None,
    Field(
        description=(
            "Values bound to the $1, $2, ... placeholders in `sql`, in order, as real "
            "driver bind parameters. Omit when the SQL has no placeholders."
        )
    ),
]

TimeoutMs = Annotated[
    int | None,
    Field(
        description=(
            "Cancel the statement if it runs longer than this many milliseconds "
            "(a positive integer). Omit for no per-call timeout."
        )
    ),
]

CursorId = Annotated[
    str,
    Field(
        description=(
            "The cursor_id returned by open_query_cursor. An id that was fully read, "
            "closed, failed or reaped after 15 idle minutes is an error."
        )
    ),
]

RowLimit = Annotated[
    int | None,
    Field(
        description=(
            "Return at most this many items; `meta.truncated` is then true when more "
            "matched. Omit to return every match."
        )
    ),
]

OutputDir = Annotated[
    str | None,
    Field(
        description=(
            "Directory on the machine running this server to write the export file into "
            "(created if missing), instead of returning it inline. Omit to return it inline."
        )
    ),
]

# ---- Tool behaviour hints ------------------------------------------------------

#: Reads the database or local config and changes nothing; repeating it is harmless.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

#: Changes nothing in the database but holds or advances server-side state (a cursor),
#: so a repeated call does not return the same thing.
READ_ONLY_STATEFUL = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

#: Reads the database; may also write an export file into `output_dir` (never
#: overwriting one: each file name carries a UTC timestamp).
READ_WITH_EXPORT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

#: Runs INSERT/UPDATE/DELETE/DDL; a committed call can change or delete data.
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)

#: Changes local state (a setting or a cursor) without touching data; repeating the
#: same call has no further effect.
LOCAL_STATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

#: Interrupts another session's running statement; its work is rolled back.
CANCEL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

#: Read-only diagnostics that may also look up the latest release on PyPI.
DIAGNOSTICS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
