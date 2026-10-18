"""The ``tooldistill`` command.

Every command is a thin call into a public library function; this module only parses arguments and
prints plain-text tables. Commands that run tools or models load the executor from ``tooldistill.toml``.
"""

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from tooldistill import __version__
from tooldistill.agent import ToolDistill
from tooldistill.core import lifecycle
from tooldistill.core.config import CONFIG_FILENAME, init_config, load_config
from tooldistill.core.errors import ToolDistillError
from tooldistill.core.models import CallKind, Status, VersionRecord
from tooldistill.core.registry import Registry

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Verified, reusable distilled tools for agents.")
traces_app = typer.Typer(no_args_is_help=True, help="Recorded runs.")
app.add_typer(traces_app, name="traces")

DEFAULT_STORE = ".tooldistill"
ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to tooldistill.toml.")]
StoreOpt = Annotated[Path | None, typer.Option("--store", "-s", help="Store folder (default: from the config file, else .tooldistill).")]
VersionOpt = Annotated[int | None, typer.Option("--version", "-v", help="Version number (default: the exposed version, else the latest).")]
LiveOpt = Annotated[bool, typer.Option("--live", help="Run every base tool call live: needs allow_live_verification and has real side effects.")]


# ------------------------------------------------------------------------------ helpers


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=1)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except ToolDistillError as exc:
        _fail(str(exc))


def _store_path(config: Path, store: Path | None) -> Path:
    if store is not None:
        return store
    if config.is_file():
        with _errors():
            return load_config(config).store
    return Path(DEFAULT_STORE)


def _registry(config: Path, store: Path | None) -> Registry:
    path = _store_path(config, store)
    if not (path / "registry.db").is_file():
        _fail(f"no store at {path} (run 'tooldistill init' or pass --store)")
    return Registry(path)


def _open(config: Path) -> ToolDistill:
    """A ToolDistill from the config file, without the startup schema check (``health`` does that explicitly)."""
    if not config.is_file():
        _fail(f"config file not found: {config} (run 'tooldistill init' or pass --config)")
    with _errors():
        return ToolDistill.from_config(config, check_schemas_on_start=False)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [max([len(header)] + [len(row[i]) for row in cells]) for i, header in enumerate(headers)]

    def line(row: Sequence[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()

    return "\n".join([line(list(headers)), line(["-" * w for w in widths]), *(line(row) for row in cells)])


def _short(text: str | None, limit: int = 60) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _status(version: VersionRecord) -> str:
    return f"{version.status} ({version.status_reason})" if version.status_reason else str(version.status)


def _message(details: str) -> str:
    try:
        return str(json.loads(details).get("message", details))
    except (ValueError, AttributeError):
        return details


# ----------------------------------------------------------------------------- commands


@app.command()
def version() -> None:
    """Print the library version."""
    typer.echo(__version__)


@app.command()
def init(config: ConfigOpt = Path(CONFIG_FILENAME), store: StoreOpt = None) -> None:
    """Create the store (with its .gitignore) and a sample tooldistill.toml."""
    store_path = store or Path(DEFAULT_STORE)
    Registry(store_path).close()
    typer.echo(f"store ready at {store_path} (registry.db* is gitignored there)")
    if init_config(config, store=store_path):
        typer.echo(f"wrote {config}: set executor and tools to your module:attr references")
    else:
        typer.echo(f"{config} already exists; left unchanged")


@app.command("list")
def list_versions(
    config: ConfigOpt = Path(CONFIG_FILENAME),
    store: StoreOpt = None,
    status: Annotated[str | None, typer.Option("--status", help="Only versions with this status.")] = None,
) -> None:
    """List distilled tool versions."""
    statuses = None
    if status is not None:
        try:
            statuses = [Status(status)]
        except ValueError:
            _fail(f"unknown status '{status}' (one of: {', '.join(s.value for s in Status)})")
    registry = _registry(config, store)
    try:
        versions = registry.list_versions(statuses=statuses)
        if not versions:
            typer.echo("no distilled tools" + (f" with status {status}" if status else ""))
            return
        rows = [
            [
                v.tool_name, v.version, v.status, v.status_reason or "", v.baseline_calls if v.baseline_calls is not None else "",
                v.measured_calls if v.measured_calls is not None else "", registry.success_count(v.tool_name, v.version), v.created_at[:19],
            ]
            for v in versions
        ]
        typer.echo(_table(["tool", "ver", "status", "reason", "baseline", "measured", "ok runs", "created"], rows))
    finally:
        registry.close()


@app.command()
def show(
    name: Annotated[str, typer.Argument(help="Distilled tool name.")],
    config: ConfigOpt = Path(CONFIG_FILENAME),
    store: StoreOpt = None,
    version: VersionOpt = None,
) -> None:
    """Show a version: metadata, dependencies, status history, check results, run stats and code."""
    registry = _registry(config, store)
    try:
        with _errors():
            record = lifecycle.select_version(registry, name, version)
        typer.echo(f"{record.label}  {_status(record)}")
        typer.echo(f"description:   {record.description}")
        typer.echo(f"created:       {record.created_at}")
        typer.echo(f"source trace:  {record.source_trace_id or '-'}")
        baseline = f"{record.baseline_calls} calls"
        if record.baseline_tokens is not None:
            baseline += f", {record.baseline_tokens} tokens"
        if record.baseline_latency_ms is not None:
            baseline += f", {record.baseline_latency_ms} ms"
        typer.echo(f"baseline:      {baseline}")
        typer.echo(f"measured:      {record.measured_calls if record.measured_calls is not None else '-'} calls")
        typer.echo(f"code:          {record.code_path}")
        deps = registry.list_dependencies(name, record.version)
        typer.echo("dependencies:  " + (", ".join(f"{d.base_tool} ({d.schema_hash[:12]})" for d in deps) or "-"))
        avoided = registry.list_avoided_tools(name, record.version)
        typer.echo("avoided:       " + (", ".join(f"{a.base_tool} ({a.reason})" for a in avoided) or "-"))
        typer.echo(f"fixtures:      {len(registry.list_fixtures(name, record.version))}")
        typer.echo(f"test inputs:   {record.test_inputs_json}")
        queued = [i for i in registry.list_rebuild_queue(name=name) if i.version == record.version]
        typer.echo("rebuild queue: " + (", ".join(f"{i.base_tool} (queued {i.queued_at[:19]})" for i in queued) or "-"))
        others = [v for v in registry.list_versions(name) if v.version != record.version]
        if others:
            typer.echo("other versions: " + ", ".join(f"v{v.version} {_status(v)}" for v in others))

        typer.echo("\nstatus history")
        events = registry.list_status_events(name, record.version)
        typer.echo(_table(["at", "from", "to", "reason"], [[e.at[:19], e.from_status or "-", e.to_status, e.reason] for e in events]))

        checks = registry.list_check_results(name, record.version)
        typer.echo("\nchecks")
        if checks:
            rows = [[c.ran_at[:19], c.check, "passed" if c.passed else "FAILED", _short(_message(c.details), 90)] for c in checks]
            typer.echo(_table(["ran at", "check", "result", "message"], rows))
        else:
            typer.echo("none yet")

        stats = registry.list_run_stats(name, record.version)
        typer.echo(f"\nruns ({len(stats)} total, {sum(1 for s in stats if s.success)} successful)")
        if stats:
            rows = [[s.ran_at[:19], "ok" if s.success else "FAILED", s.calls, s.latency_ms if s.latency_ms is not None else "", _short(s.error)] for s in stats[-10:]]
            typer.echo(_table(["ran at", "result", "calls", "ms", "error"], rows))

        typer.echo("\ncode\n----")
        typer.echo(registry.read_code(record))
    finally:
        registry.close()


@traces_app.command("list")
def traces_list(
    config: ConfigOpt = Path(CONFIG_FILENAME),
    store: StoreOpt = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many recent traces to show.")] = 20,
) -> None:
    """List recent runs."""
    registry = _registry(config, store)
    try:
        traces = registry.list_traces(limit=limit)
        if not traces:
            typer.echo("no traces")
            return
        rows = []
        for t in traces:
            calls = registry.list_tool_calls(t.id)
            base = sum(1 for c in calls if c.kind is CallKind.BASE and c.parent_call_id is None)
            distilled = sum(1 for c in calls if c.kind is CallKind.DISTILLED)
            proposals = sum(1 for c in calls if c.kind is CallKind.PROPOSE)
            outcome = "-" if t.outcome is None else ("success" if t.outcome else "failure")
            rows.append([t.id, t.started_at[:19], outcome, base, distilled, proposals, t.input_tokens + t.output_tokens, "yes" if t.nudged else "", _short(t.task, 50)])
        typer.echo(_table(["trace", "started", "outcome", "base", "distilled", "proposed", "tokens", "nudged", "task"], rows))
    finally:
        registry.close()


@traces_app.command("show")
def traces_show(
    trace_id: Annotated[str, typer.Argument(help="Trace id (a unique prefix is enough).")],
    config: ConfigOpt = Path(CONFIG_FILENAME),
    store: StoreOpt = None,
) -> None:
    """Show one run and every tool call it made."""
    registry = _registry(config, store)
    try:
        trace = registry.get_trace(trace_id)
        if trace is None:
            matches = [t for t in registry.list_traces(limit=1000) if t.id.startswith(trace_id)]
            if len(matches) != 1:
                _fail(f"no trace '{trace_id}'" if not matches else f"'{trace_id}' matches {len(matches)} traces")
            trace = matches[0]
        outcome = "-" if trace.outcome is None else ("success" if trace.outcome else "failure")
        typer.echo(f"trace {trace.id}")
        typer.echo(f"task:      {trace.task}")
        typer.echo(f"started:   {trace.started_at}")
        typer.echo(f"finished:  {trace.finished_at or '-'}")
        typer.echo(f"outcome:   {outcome}")
        typer.echo(f"tokens:    {trace.input_tokens} in / {trace.output_tokens} out")
        typer.echo(f"nudged:    {'yes' if trace.nudged else 'no'}")
        versions = registry.versions_from_trace(trace.id)
        if versions:
            typer.echo("distilled: " + ", ".join(f"{v.label} {_status(v)}" for v in versions))
        calls = registry.list_tool_calls(trace.id)
        typer.echo(f"\ntool calls ({len(calls)})")
        rows = []
        for c in calls:
            kind = f"{c.kind}" + (f" (in #{c.parent_call_id})" if c.parent_call_id is not None else "")
            rows.append([c.seq, kind, c.tool_name, c.version or "", "ERROR" if c.is_error else "ok", c.latency_ms if c.latency_ms is not None else "", _short(c.input_json, 50), _short(c.error if c.is_error else c.output_json, 50)])
        if rows:
            typer.echo(_table(["seq", "kind", "tool", "ver", "result", "ms", "input", "output / error"], rows))
    finally:
        registry.close()


@app.command()
def promote(
    name: Annotated[str, typer.Argument(help="Distilled tool name.")],
    config: ConfigOpt = Path(CONFIG_FILENAME),
    version: VersionOpt = None,
    live: LiveOpt = False,
) -> None:
    """Activate a version (the highest verified one by default). Other versions are re-verified first."""
    td = _open(config)
    try:
        with _errors():
            result = td.promote(name, version, live=live)
        typer.echo(result.message)
        if not result.activated:
            raise typer.Exit(code=1)
    finally:
        td.close()


@app.command()
def deprecate(
    name: Annotated[str, typer.Argument(help="Distilled tool name.")],
    config: ConfigOpt = Path(CONFIG_FILENAME),
    store: StoreOpt = None,
    version: VersionOpt = None,
) -> None:
    """Deprecate a version (the exposed one by default, else the latest)."""
    registry = _registry(config, store)
    try:
        with _errors():
            record = lifecycle.select_version(registry, name, version)
            updated = lifecycle.deprecate(registry, name, record.version, "manual")
        if updated is None:
            typer.echo(f"{record.label} is already deprecated")
        else:
            typer.echo(f"{updated.label} is now deprecated")
    finally:
        registry.close()


@app.command()
def verify(
    name: Annotated[str, typer.Argument(help="Distilled tool name.")],
    config: ConfigOpt = Path(CONFIG_FILENAME),
    version: VersionOpt = None,
    live: LiveOpt = False,
) -> None:
    """Re-run the static, replay and efficiency checks on a version and apply the outcome."""
    td = _open(config)
    try:
        with _errors():
            record, report = td.verify(name, version, live=live)
        typer.echo(f"{record.label}: {report.verdict}")
        for note in report.notes:
            typer.echo(f"note: {note}")
        typer.echo(f"status: {_status(record)}")
        if not report.passed:
            raise typer.Exit(code=1)
    finally:
        td.close()


@app.command("tool-updated")
def tool_updated(
    base_tool: Annotated[str, typer.Argument(help="The base tool that changed or came back.")],
    config: ConfigOpt = Path(CONFIG_FILENAME),
) -> None:
    """Tell the library a base tool changed schema or recovered: re-verify dependents, queue rebuilds."""
    td = _open(config)
    try:
        with _errors():
            report = td.on_tool_updated(base_tool)
        if report.current_hash is None:
            typer.echo(f"{base_tool} is not in the current tool list")
        if not report.actions:
            typer.echo(f"no exposed version depends on or avoided {base_tool} with a different schema; nothing to do")
            return
        for a in report.actions:
            typer.echo(f"{a.tool_name} v{a.version}: {a.from_status} -> {a.to_status} ({a.reason}) {a.detail}")
    finally:
        td.close()


@app.command()
def health(
    config: ConfigOpt = Path(CONFIG_FILENAME),
    no_probe: Annotated[bool, typer.Option("--no-probe", help="Compare schemas only; skip the health probe.")] = False,
) -> None:
    """Detect schema changes, outages and recoveries of base tools and react to them."""
    td = _open(config)
    try:
        with _errors():
            report = td.health(probe=not no_probe)
        typer.echo("changed schemas: " + (", ".join(report.changed) or "none"))
        if report.missing:
            typer.echo("missing tools:   " + ", ".join(report.missing))
        if td.health_probe is None or no_probe:
            typer.echo("probe:           " + ("skipped" if no_probe else "not configured (set health_probe in the config)"))
        else:
            typer.echo("recovered:       " + (", ".join(report.recovered) or "none"))
            typer.echo("down:            " + (", ".join(report.down) or "none"))
        for a in report.actions:
            typer.echo(f"{a.tool_name} v{a.version}: {a.from_status} -> {a.to_status} ({a.reason}) {a.detail}")
    finally:
        td.close()


@app.command()
def rebuild(
    name: Annotated[str, typer.Argument(help="Distilled tool name with an open rebuild item.")],
    config: ConfigOpt = Path(CONFIG_FILENAME),
) -> None:
    """Rebuild a queued distilled tool with the Distiller; the new version is verified like any other."""
    td = _open(config)
    try:
        with _errors():
            result = td.rebuild(name)
        typer.echo(result.message)
        if not result.rebuilt:
            raise typer.Exit(code=1)
    finally:
        td.close()
