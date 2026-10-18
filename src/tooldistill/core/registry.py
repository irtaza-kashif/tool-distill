"""The registry: the only module that reads or writes the store (SQLite database + distilled tool files).

Every public method is one short transaction. Nothing here runs a tool or calls a model, so no
transaction is ever held open across either.
"""

import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tooldistill.core.errors import RegistryError
from tooldistill.core.models import (
    AvoidedTool,
    CallKind,
    CheckName,
    CheckResult,
    Dependency,
    DistilledToolRecord,
    Fixture,
    RebuildItem,
    RunStat,
    Status,
    StatusEvent,
    ToolCallRecord,
    TraceData,
    TraceRecord,
    VersionRecord,
)
from tooldistill.core.serialization import code_hash, now_iso

__all__ = ["DB_FILENAME", "GITIGNORE_TEXT", "SCHEMA_VERSION", "TOOLS_DIRNAME", "Registry"]

DB_FILENAME = "registry.db"
TOOLS_DIRNAME = "tools"
GITIGNORE_TEXT = (
    "# tooldistill store. The database holds traces and fixtures with real tool outputs,\n"
    "# which may include personal data. Distilled tool .py files may be committed.\n"
    "registry.db*\n"
)
SCHEMA_VERSION = 1

_VERSION_FK = "FOREIGN KEY (tool_name, version) REFERENCES versions(tool_name, version) ON DELETE CASCADE"

_DDL_V1: tuple[str, ...] = (
    """CREATE TABLE traces (
        id TEXT PRIMARY KEY,
        task TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome INTEGER,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        nudged INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        parent_call_id INTEGER REFERENCES tool_calls(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('base', 'distilled', 'propose')),
        tool_name TEXT NOT NULL,
        version INTEGER,
        input_json TEXT NOT NULL,
        output_json TEXT,
        is_error INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        latency_ms INTEGER,
        UNIQUE (trace_id, seq)
    )""",
    "CREATE INDEX ix_tool_calls_parent ON tool_calls(parent_call_id)",
    """CREATE TABLE distilled_tools (
        name TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE versions (
        tool_name TEXT NOT NULL REFERENCES distilled_tools(name) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('draft', 'verified', 'active', 'suspect', 'deprecated')),
        status_reason TEXT,
        description TEXT NOT NULL,
        input_schema_json TEXT NOT NULL,
        code_path TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        source_trace_id TEXT REFERENCES traces(id) ON DELETE SET NULL,
        test_inputs_json TEXT NOT NULL,
        baseline_calls INTEGER,
        baseline_tokens INTEGER,
        baseline_latency_ms INTEGER,
        measured_calls INTEGER,
        created_at TEXT NOT NULL,
        PRIMARY KEY (tool_name, version)
    )""",
    "CREATE UNIQUE INDEX ux_versions_one_active ON versions(tool_name) WHERE status = 'active'",
    "CREATE INDEX ix_versions_status ON versions(status, status_reason)",
    f"""CREATE TABLE dependencies (
        tool_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        base_tool TEXT NOT NULL,
        schema_hash TEXT NOT NULL,
        PRIMARY KEY (tool_name, version, base_tool),
        {_VERSION_FK}
    )""",
    "CREATE INDEX ix_dependencies_base ON dependencies(base_tool)",
    f"""CREATE TABLE avoided_tools (
        tool_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        base_tool TEXT NOT NULL,
        reason TEXT NOT NULL,
        PRIMARY KEY (tool_name, version, base_tool),
        {_VERSION_FK}
    )""",
    "CREATE INDEX ix_avoided_base ON avoided_tools(base_tool)",
    f"""CREATE TABLE fixtures (
        tool_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        base_tool TEXT NOT NULL,
        input_json TEXT NOT NULL,
        output_json TEXT NOT NULL,
        PRIMARY KEY (tool_name, version, seq),
        {_VERSION_FK}
    )""",
    f"""CREATE TABLE check_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        check_name TEXT NOT NULL CHECK (check_name IN ('static', 'replay', 'efficiency')),
        passed INTEGER NOT NULL,
        details TEXT NOT NULL,
        ran_at TEXT NOT NULL,
        {_VERSION_FK}
    )""",
    "CREATE INDEX ix_check_results_version ON check_results(tool_name, version, id)",
    f"""CREATE TABLE run_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        trace_id TEXT,
        call_id INTEGER,
        ran_at TEXT NOT NULL,
        success INTEGER NOT NULL,
        calls INTEGER NOT NULL,
        latency_ms INTEGER,
        error TEXT,
        {_VERSION_FK}
    )""",
    "CREATE INDEX ix_run_stats_version ON run_stats(tool_name, version, success, ran_at)",
    "CREATE INDEX ix_run_stats_trace ON run_stats(trace_id)",
    f"""CREATE TABLE status_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        reason TEXT NOT NULL,
        at TEXT NOT NULL,
        {_VERSION_FK}
    )""",
    "CREATE INDEX ix_status_events_version ON status_events(tool_name, version, id)",
    f"""CREATE TABLE rebuild_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        reason TEXT NOT NULL,
        base_tool TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        resolved_at TEXT,
        {_VERSION_FK}
    )""",
    "CREATE UNIQUE INDEX ux_rebuild_open ON rebuild_queue(tool_name, version, base_tool) WHERE resolved_at IS NULL",
)

# MIGRATIONS[i] brings a store at user_version == i to i + 1.
MIGRATIONS: tuple[tuple[str, ...], ...] = (_DDL_V1,)


def _row_trace(row: sqlite3.Row) -> TraceRecord:
    return TraceRecord(
        id=row["id"],
        task=row["task"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outcome=None if row["outcome"] is None else bool(row["outcome"]),
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        nudged=bool(row["nudged"]),
    )


def _row_call(row: sqlite3.Row) -> ToolCallRecord:
    return ToolCallRecord(
        id=row["id"],
        trace_id=row["trace_id"],
        seq=row["seq"],
        parent_call_id=row["parent_call_id"],
        kind=CallKind(row["kind"]),
        tool_name=row["tool_name"],
        version=row["version"],
        input_json=row["input_json"],
        output_json=row["output_json"],
        is_error=bool(row["is_error"]),
        error=row["error"],
        latency_ms=row["latency_ms"],
    )


def _row_version(row: sqlite3.Row) -> VersionRecord:
    return VersionRecord(
        tool_name=row["tool_name"],
        version=row["version"],
        status=Status(row["status"]),
        status_reason=row["status_reason"],
        description=row["description"],
        input_schema_json=row["input_schema_json"],
        code_path=row["code_path"],
        code_hash=row["code_hash"],
        source_trace_id=row["source_trace_id"],
        test_inputs_json=row["test_inputs_json"],
        baseline_calls=row["baseline_calls"],
        baseline_tokens=row["baseline_tokens"],
        baseline_latency_ms=row["baseline_latency_ms"],
        measured_calls=row["measured_calls"],
        created_at=row["created_at"],
    )


def _row_run_stat(row: sqlite3.Row) -> RunStat:
    return RunStat(
        id=row["id"],
        tool_name=row["tool_name"],
        version=row["version"],
        trace_id=row["trace_id"],
        call_id=row["call_id"],
        ran_at=row["ran_at"],
        success=bool(row["success"]),
        calls=row["calls"],
        latency_ms=row["latency_ms"],
        error=row["error"],
    )


def _row_rebuild(row: sqlite3.Row) -> RebuildItem:
    return RebuildItem(
        id=row["id"],
        tool_name=row["tool_name"],
        version=row["version"],
        reason=row["reason"],
        base_tool=row["base_tool"],
        queued_at=row["queued_at"],
        resolved_at=row["resolved_at"],
    )


class Registry:
    """Reads and writes the ``.tooldistill/`` store."""

    def __init__(self, store: str | Path) -> None:
        self.path = Path(store).expanduser().resolve()
        self.tools_dir = self.path / TOOLS_DIRNAME
        self.db_path = self.path / DB_FILENAME
        self.path.mkdir(parents=True, exist_ok=True)
        self.tools_dir.mkdir(exist_ok=True)
        gitignore = self.path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(GITIGNORE_TEXT, encoding="utf-8")
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(str(self.db_path), autocommit=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ----------------------------------------------------------------- plumbing

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (or ``ROLLBACK``); re-entrant, so helpers can nest."""
        with self._lock:
            if self._depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("COMMIT")

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _row(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self._row(sql, params)
        return None if row is None else row[0]

    def _migrate(self) -> None:
        with self.transaction() as conn:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > SCHEMA_VERSION:
                raise RegistryError(
                    f"store schema version {current} is newer than this library supports ({SCHEMA_VERSION})"
                )
            for step in range(current, SCHEMA_VERSION):
                for statement in MIGRATIONS[step]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {step + 1}")

    def pragmas(self) -> dict[str, Any]:
        """Connection settings, for diagnostics and tests."""
        return {
            "journal_mode": self._scalar("PRAGMA journal_mode"),
            "busy_timeout": self._scalar("PRAGMA busy_timeout"),
            "foreign_keys": self._scalar("PRAGMA foreign_keys"),
            "user_version": self._scalar("PRAGMA user_version"),
        }

    @staticmethod
    def _expect(cursor: sqlite3.Cursor, what: str) -> None:
        if cursor.rowcount == 0:
            raise RegistryError(f"{what} not found")

    # ------------------------------------------------------------------- traces

    def create_trace(self, task: str, *, trace_id: str | None = None) -> TraceRecord:
        tid = trace_id or uuid.uuid7().hex
        now = now_iso()
        with self.transaction() as conn:
            conn.execute("INSERT INTO traces (id, task, started_at) VALUES (?, ?, ?)", (tid, task, now))
        return TraceRecord(tid, task, now, None, None, 0, 0, False)

    def get_trace(self, trace_id: str) -> TraceRecord | None:
        row = self._row("SELECT * FROM traces WHERE id = ?", (trace_id,))
        return None if row is None else _row_trace(row)

    def require_trace(self, trace_id: str) -> TraceRecord:
        trace = self.get_trace(trace_id)
        if trace is None:
            raise RegistryError(f"unknown trace '{trace_id}'")
        return trace

    def list_traces(self, limit: int = 50) -> list[TraceRecord]:
        rows = self._rows("SELECT * FROM traces ORDER BY started_at DESC, id DESC LIMIT ?", (limit,))
        return [_row_trace(r) for r in rows]

    def set_outcome(self, trace_id: str, success: bool) -> None:
        with self.transaction() as conn:
            cursor = conn.execute("UPDATE traces SET outcome = ? WHERE id = ?", (int(success), trace_id))
            self._expect(cursor, f"trace '{trace_id}'")

    def add_usage(self, trace_id: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE traces SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ? WHERE id = ?",
                (int(input_tokens), int(output_tokens), trace_id),
            )
            self._expect(cursor, f"trace '{trace_id}'")

    def mark_nudged(self, trace_id: str) -> None:
        with self.transaction() as conn:
            self._expect(conn.execute("UPDATE traces SET nudged = 1 WHERE id = ?", (trace_id,)), f"trace '{trace_id}'")

    def finish_trace(self, trace_id: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute("UPDATE traces SET finished_at = ? WHERE id = ?", (now_iso(), trace_id))
            self._expect(cursor, f"trace '{trace_id}'")

    def get_trace_data(self, trace_id: str) -> TraceData:
        return TraceData(trace=self.require_trace(trace_id), calls=self.list_tool_calls(trace_id))

    # --------------------------------------------------------------- tool calls

    def add_tool_call(
        self,
        trace_id: str,
        *,
        kind: CallKind,
        tool_name: str,
        input_json: str,
        output_json: str | None = None,
        is_error: bool = False,
        error: str | None = None,
        latency_ms: int | None = None,
        parent_call_id: int | None = None,
        version: int | None = None,
    ) -> ToolCallRecord:
        with self.transaction() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM tool_calls WHERE trace_id = ?", (trace_id,)
            ).fetchone()[0]
            cursor = conn.execute(
                """INSERT INTO tool_calls
                   (trace_id, seq, parent_call_id, kind, tool_name, version, input_json, output_json,
                    is_error, error, latency_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trace_id, seq, parent_call_id, str(kind), tool_name, version, input_json, output_json,
                    int(is_error), error, latency_ms,
                ),
            )
            call_id = cursor.lastrowid
        assert call_id is not None
        return ToolCallRecord(
            call_id, trace_id, seq, parent_call_id, CallKind(kind), tool_name, version, input_json, output_json,
            is_error, error, latency_ms,
        )

    def update_tool_call(
        self,
        call_id: int,
        *,
        output_json: str | None = None,
        is_error: bool | None = None,
        error: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if output_json is not None:
            sets.append("output_json = ?")
            params.append(output_json)
        if is_error is not None:
            sets.append("is_error = ?")
            params.append(int(is_error))
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if latency_ms is not None:
            sets.append("latency_ms = ?")
            params.append(latency_ms)
        if not sets:
            return
        params.append(call_id)
        with self.transaction() as conn:
            cursor = conn.execute(f"UPDATE tool_calls SET {', '.join(sets)} WHERE id = ?", params)
            self._expect(cursor, f"tool call {call_id}")

    def get_tool_call(self, call_id: int) -> ToolCallRecord | None:
        row = self._row("SELECT * FROM tool_calls WHERE id = ?", (call_id,))
        return None if row is None else _row_call(row)

    def list_tool_calls(self, trace_id: str) -> list[ToolCallRecord]:
        rows = self._rows("SELECT * FROM tool_calls WHERE trace_id = ? ORDER BY seq", (trace_id,))
        return [_row_call(r) for r in rows]

    # ------------------------------------------------------------ distilled tools

    def get_tool(self, name: str) -> DistilledToolRecord | None:
        row = self._row("SELECT * FROM distilled_tools WHERE name = ?", (name,))
        return None if row is None else DistilledToolRecord(row["name"], row["description"], row["created_at"])

    def list_tools(self) -> list[DistilledToolRecord]:
        rows = self._rows("SELECT * FROM distilled_tools ORDER BY name")
        return [DistilledToolRecord(r["name"], r["description"], r["created_at"]) for r in rows]

    def set_tool_description(self, name: str, description: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute("UPDATE distilled_tools SET description = ? WHERE name = ?", (description, name))
            self._expect(cursor, f"distilled tool '{name}'")

    def create_version(
        self,
        *,
        name: str,
        description: str,
        input_schema_json: str,
        code: str,
        test_inputs_json: str,
        source_trace_id: str | None,
        baseline_calls: int | None = None,
        baseline_tokens: int | None = None,
        baseline_latency_ms: int | None = None,
        status_reason: str = "pending_verification",
    ) -> VersionRecord:
        """Create the next version of a distilled tool as a draft and write its code file.

        The ``(none -> draft)`` status event is written here, in the same transaction as the row:
        it is the creation of the version, not a status change, so it does not go through lifecycle.
        """
        now = now_iso()
        digest = code_hash(code)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO distilled_tools (name, description, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO NOTHING",
                (name, description, now),
            )
            number = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM versions WHERE tool_name = ?", (name,)
            ).fetchone()[0]
            rel_path = f"{TOOLS_DIRNAME}/{name}/v{number}.py"
            conn.execute(
                """INSERT INTO versions
                   (tool_name, version, status, status_reason, description, input_schema_json, code_path, code_hash,
                    source_trace_id, test_inputs_json, baseline_calls, baseline_tokens, baseline_latency_ms,
                    measured_calls, created_at)
                   VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    name, number, status_reason, description, input_schema_json, rel_path, digest,
                    source_trace_id, test_inputs_json, baseline_calls, baseline_tokens, baseline_latency_ms, now,
                ),
            )
            conn.execute(
                "INSERT INTO status_events (tool_name, version, from_status, to_status, reason, at) "
                "VALUES (?, ?, NULL, 'draft', ?, ?)",
                (name, number, status_reason, now),
            )
            file = self.path / rel_path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(code, encoding="utf-8", newline="\n")
        return VersionRecord(
            name, number, Status.DRAFT, status_reason, description, input_schema_json, rel_path, digest,
            source_trace_id, test_inputs_json, baseline_calls, baseline_tokens, baseline_latency_ms, None, now,
        )

    def get_version(self, name: str, version: int) -> VersionRecord | None:
        row = self._row("SELECT * FROM versions WHERE tool_name = ? AND version = ?", (name, version))
        return None if row is None else _row_version(row)

    def require_version(self, name: str, version: int) -> VersionRecord:
        record = self.get_version(name, version)
        if record is None:
            raise RegistryError(f"unknown distilled tool version '{name}' v{version}")
        return record

    def list_versions(
        self, name: str | None = None, *, statuses: Iterable[Status] | None = None
    ) -> list[VersionRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if name is not None:
            clauses.append("tool_name = ?")
            params.append(name)
        if statuses is not None:
            wanted = [str(s) for s in statuses]
            if not wanted:
                return []
            clauses.append(f"status IN ({', '.join('?' * len(wanted))})")
            params.extend(wanted)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(f"SELECT * FROM versions {where} ORDER BY tool_name, version", params)
        return [_row_version(r) for r in rows]

    def latest_version(self, name: str) -> VersionRecord | None:
        row = self._row("SELECT * FROM versions WHERE tool_name = ? ORDER BY version DESC LIMIT 1", (name,))
        return None if row is None else _row_version(row)

    def active_version(self, name: str) -> VersionRecord | None:
        row = self._row("SELECT * FROM versions WHERE tool_name = ? AND status = 'active'", (name,))
        return None if row is None else _row_version(row)

    def set_status(self, name: str, version: int, status: Status, reason: str | None) -> None:
        """Write a version's status. **Only ``lifecycle.py`` may call this**; it owns every transition."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE versions SET status = ?, status_reason = ? WHERE tool_name = ? AND version = ?",
                (str(status), reason, name, version),
            )
            self._expect(cursor, f"version '{name}' v{version}")

    def update_version_metadata(self, name: str, version: int, *, description: str, input_schema_json: str) -> None:
        """Sync a version's description and schema from its decorator once the static check has read them."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE versions SET description = ?, input_schema_json = ? WHERE tool_name = ? AND version = ?",
                (description, input_schema_json, name, version),
            )
            self._expect(cursor, f"version '{name}' v{version}")

    def versions_from_trace(self, trace_id: str) -> list[VersionRecord]:
        rows = self._rows("SELECT * FROM versions WHERE source_trace_id = ? ORDER BY tool_name, version", (trace_id,))
        return [_row_version(r) for r in rows]

    def set_measured_calls(self, name: str, version: int, measured_calls: int | None) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE versions SET measured_calls = ? WHERE tool_name = ? AND version = ?",
                (measured_calls, name, version),
            )
            self._expect(cursor, f"version '{name}' v{version}")

    def code_file(self, version: VersionRecord) -> Path:
        return self.path / version.code_path

    def read_code(self, version: VersionRecord) -> str:
        return self.code_file(version).read_text(encoding="utf-8")

    def exposed_versions(self, *, suspect_reasons: Sequence[str], limit: int | None) -> list[VersionRecord]:
        """Active versions plus suspect versions with one of ``suspect_reasons``, best first.

        Which statuses count as exposed is lifecycle's decision; this only runs the query:
        ordered by successful run count, then last successful run, then creation time.
        """
        reason_clause = ""
        params: list[Any] = []
        if suspect_reasons:
            placeholders = ", ".join("?" * len(suspect_reasons))
            reason_clause = f" OR (v.status = 'suspect' AND v.status_reason IN ({placeholders}))"
            params.extend(suspect_reasons)
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(limit)
        rows = self._rows(
            f"""SELECT v.*, COALESCE(s.ok_runs, 0) AS ok_runs, s.last_ok AS last_ok
                FROM versions v
                LEFT JOIN (
                    SELECT tool_name, version, SUM(success) AS ok_runs,
                           MAX(CASE WHEN success = 1 THEN ran_at END) AS last_ok
                    FROM run_stats GROUP BY tool_name, version
                ) s ON s.tool_name = v.tool_name AND s.version = v.version
                WHERE v.status = 'active'{reason_clause}
                ORDER BY ok_runs DESC, COALESCE(s.last_ok, v.created_at) DESC, v.created_at DESC, v.tool_name
                {limit_clause}""",
            params,
        )
        return [_row_version(r) for r in rows]

    # ----------------------------------------- dependencies / avoided / fixtures

    def replace_dependencies(self, name: str, version: int, hashes: Mapping[str, str]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM dependencies WHERE tool_name = ? AND version = ?", (name, version))
            conn.executemany(
                "INSERT INTO dependencies (tool_name, version, base_tool, schema_hash) VALUES (?, ?, ?, ?)",
                [(name, version, base_tool, digest) for base_tool, digest in sorted(hashes.items())],
            )

    def list_dependencies(self, name: str, version: int) -> list[Dependency]:
        rows = self._rows(
            "SELECT * FROM dependencies WHERE tool_name = ? AND version = ? ORDER BY base_tool", (name, version)
        )
        return [Dependency(r["tool_name"], r["version"], r["base_tool"], r["schema_hash"]) for r in rows]

    def versions_depending_on(self, base_tool: str) -> list[Dependency]:
        rows = self._rows(
            "SELECT * FROM dependencies WHERE base_tool = ? ORDER BY tool_name, version", (base_tool,)
        )
        return [Dependency(r["tool_name"], r["version"], r["base_tool"], r["schema_hash"]) for r in rows]

    def replace_avoided_tools(self, name: str, version: int, avoided: Mapping[str, str]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM avoided_tools WHERE tool_name = ? AND version = ?", (name, version))
            conn.executemany(
                "INSERT INTO avoided_tools (tool_name, version, base_tool, reason) VALUES (?, ?, ?, ?)",
                [(name, version, base_tool, reason) for base_tool, reason in sorted(avoided.items())],
            )

    def list_avoided_tools(self, name: str, version: int) -> list[AvoidedTool]:
        rows = self._rows(
            "SELECT * FROM avoided_tools WHERE tool_name = ? AND version = ? ORDER BY base_tool", (name, version)
        )
        return [AvoidedTool(r["tool_name"], r["version"], r["base_tool"], r["reason"]) for r in rows]

    def versions_avoiding(self, base_tool: str) -> list[AvoidedTool]:
        rows = self._rows(
            "SELECT * FROM avoided_tools WHERE base_tool = ? ORDER BY tool_name, version", (base_tool,)
        )
        return [AvoidedTool(r["tool_name"], r["version"], r["base_tool"], r["reason"]) for r in rows]

    def replace_fixtures(self, name: str, version: int, fixtures: Iterable[tuple[str, str, str]]) -> list[Fixture]:
        """Store ``(base_tool, input_json, output_json)`` triples as the version's fixtures, numbered from 1."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM fixtures WHERE tool_name = ? AND version = ?", (name, version))
            return self._insert_fixtures(conn, name, version, 1, fixtures)

    def add_fixtures(self, name: str, version: int, fixtures: Iterable[tuple[str, str, str]]) -> list[Fixture]:
        """Append fixtures after the existing ones (used by live verification)."""
        with self.transaction() as conn:
            start = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM fixtures WHERE tool_name = ? AND version = ?", (name, version)
            ).fetchone()[0]
            return self._insert_fixtures(conn, name, version, start, fixtures)

    @staticmethod
    def _insert_fixtures(
        conn: sqlite3.Connection, name: str, version: int, start: int, fixtures: Iterable[tuple[str, str, str]]
    ) -> list[Fixture]:
        records = [
            Fixture(name, version, start + i, base_tool, input_json, output_json)
            for i, (base_tool, input_json, output_json) in enumerate(fixtures)
        ]
        conn.executemany(
            "INSERT INTO fixtures (tool_name, version, seq, base_tool, input_json, output_json) VALUES (?, ?, ?, ?, ?, ?)",
            [(f.tool_name, f.version, f.seq, f.base_tool, f.input_json, f.output_json) for f in records],
        )
        return records

    def list_fixtures(self, name: str, version: int) -> list[Fixture]:
        rows = self._rows("SELECT * FROM fixtures WHERE tool_name = ? AND version = ? ORDER BY seq", (name, version))
        return [
            Fixture(r["tool_name"], r["version"], r["seq"], r["base_tool"], r["input_json"], r["output_json"])
            for r in rows
        ]

    # ------------------------------------------- checks / stats / events / queue

    def add_check_result(self, name: str, version: int, check: CheckName, passed: bool, details: str) -> CheckResult:
        now = now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO check_results (tool_name, version, check_name, passed, details, ran_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, version, str(check), int(passed), details, now),
            )
            row_id = cursor.lastrowid
        assert row_id is not None
        return CheckResult(row_id, name, version, CheckName(check), passed, details, now)

    def list_check_results(self, name: str, version: int) -> list[CheckResult]:
        rows = self._rows(
            "SELECT * FROM check_results WHERE tool_name = ? AND version = ? ORDER BY id", (name, version)
        )
        return [
            CheckResult(
                r["id"], r["tool_name"], r["version"], CheckName(r["check_name"]), bool(r["passed"]), r["details"], r["ran_at"]
            )
            for r in rows
        ]

    def add_run_stat(
        self,
        name: str,
        version: int,
        *,
        success: bool,
        calls: int,
        latency_ms: int | None = None,
        error: str | None = None,
        trace_id: str | None = None,
        call_id: int | None = None,
        ran_at: str | None = None,
    ) -> RunStat:
        at = ran_at or now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO run_stats (tool_name, version, trace_id, call_id, ran_at, success, calls, latency_ms, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, version, trace_id, call_id, at, int(success), calls, latency_ms, error),
            )
            row_id = cursor.lastrowid
        assert row_id is not None
        return RunStat(row_id, name, version, trace_id, call_id, at, success, calls, latency_ms, error)

    def list_run_stats(self, name: str, version: int) -> list[RunStat]:
        rows = self._rows("SELECT * FROM run_stats WHERE tool_name = ? AND version = ? ORDER BY id", (name, version))
        return [_row_run_stat(r) for r in rows]

    def run_stats_for_trace(self, trace_id: str) -> list[RunStat]:
        rows = self._rows("SELECT * FROM run_stats WHERE trace_id = ? ORDER BY id", (trace_id,))
        return [_row_run_stat(r) for r in rows]

    def success_count(self, name: str, version: int) -> int:
        count = self._scalar(
            "SELECT COUNT(*) FROM run_stats WHERE tool_name = ? AND version = ? AND success = 1", (name, version)
        )
        return int(count or 0)

    def add_status_event(
        self, name: str, version: int, from_status: Status | None, to_status: Status, reason: str
    ) -> StatusEvent:
        """Append to the audit log. **Only ``lifecycle.py`` may call this** (``create_version`` writes the first event)."""
        now = now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO status_events (tool_name, version, from_status, to_status, reason, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, version, None if from_status is None else str(from_status), str(to_status), reason, now),
            )
            row_id = cursor.lastrowid
        assert row_id is not None
        return StatusEvent(row_id, name, version, from_status, to_status, reason, now)

    def list_status_events(self, name: str, version: int | None = None) -> list[StatusEvent]:
        if version is None:
            rows = self._rows("SELECT * FROM status_events WHERE tool_name = ? ORDER BY id", (name,))
        else:
            rows = self._rows(
                "SELECT * FROM status_events WHERE tool_name = ? AND version = ? ORDER BY id", (name, version)
            )
        return [
            StatusEvent(
                r["id"],
                r["tool_name"],
                r["version"],
                None if r["from_status"] is None else Status(r["from_status"]),
                Status(r["to_status"]),
                r["reason"],
                r["at"],
            )
            for r in rows
        ]

    def enqueue_rebuild(self, name: str, version: int, reason: str, base_tool: str) -> RebuildItem | None:
        """Queue a rebuild; returns None when an identical item is already open."""
        now = now_iso()
        with self.transaction() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO rebuild_queue (tool_name, version, reason, base_tool, queued_at) VALUES (?, ?, ?, ?, ?)",
                    (name, version, reason, base_tool, now),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc):
                    return None
                raise
            row_id = cursor.lastrowid
        assert row_id is not None
        return RebuildItem(row_id, name, version, reason, base_tool, now, None)

    def resolve_rebuild(self, item_id: int) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE rebuild_queue SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL", (now_iso(), item_id)
            )
            self._expect(cursor, f"open rebuild item {item_id}")

    def list_rebuild_queue(self, *, include_resolved: bool = False, name: str | None = None) -> list[RebuildItem]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_resolved:
            clauses.append("resolved_at IS NULL")
        if name is not None:
            clauses.append("tool_name = ?")
            params.append(name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(f"SELECT * FROM rebuild_queue {where} ORDER BY id", params)
        return [_row_rebuild(r) for r in rows]
