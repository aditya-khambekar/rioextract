#!/usr/bin/env python3

import io
import json
import stat as stat_mod
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional

import paramiko
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich import print as rprint

app = typer.Typer(
    name="rioextract",
    help="A simple CLI tool intended for FRC teams to extract log files from the RoboRIO.",
    add_completion=False,
)

console = Console()

DEFAULT_HOST_TEMPLATE = "roboRIO-{team}-frc.local"
DEFAULT_REMOTE_PATH = "/home/lvuser"
DEFAULT_PORT = 22
DEFAULT_USER = "lvuser"
SESSION_FILE = Path.home() / ".roborio_session.json"

SYSID_STATES = {
    "quasistatic-forward",
    "quasistatic-reverse",
    "dynamic-forward",
    "dynamic-reverse",
}


def _load_session() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except Exception:
        return {}


def _save_session(data: dict):
    current = _load_session()
    current.update({k: v for k, v in data.items() if v is not None})
    SESSION_FILE.write_text(json.dumps(current, indent=2))


def _resolve(
        key: str,
        provided,
        fallback=None,
        *,
        save: bool = True,
) -> Optional[str]:
    session = _load_session()
    value = provided if provided is not None else session.get(key, fallback)
    if save and value is not None:
        _save_session({key: value})
    return value


def _resolved_connection(
        team: Optional[int],
        host: Optional[str],
        port: int,
        path: Optional[str],
) -> tuple[str, int, str]:
    session = _load_session()

    if host:
        resolved_host = host
    elif team is not None:
        resolved_host = DEFAULT_HOST_TEMPLATE.format(team=team)
    elif "host" in session:
        resolved_host = session["host"]
        console.print(f"[dim]Using saved host [bold]{resolved_host}[/bold][/dim]")
    elif "team" in session:
        resolved_host = DEFAULT_HOST_TEMPLATE.format(team=session["team"])
        console.print(f"[dim]Using saved team [bold]{session['team']}[/bold] → [bold]{resolved_host}[/bold][/dim]")
    else:
        rprint(
            "[bold red]✗ No team number or host specified.[/bold red]\n"
            "[dim]Pass a team number as the first argument, or run [bold]session set[/bold] first.[/dim]"
        )
        raise typer.Exit(1)

    resolved_path = path or session.get("path", DEFAULT_REMOTE_PATH)
    if not path and "path" in session:
        console.print(f"[dim]Using saved path [bold]{resolved_path}[/bold][/dim]")

    updates: dict = {"host": resolved_host, "path": resolved_path}
    if team is not None:
        updates["team"] = team
    _save_session(updates)

    return resolved_host, port, resolved_path


session_app = typer.Typer(help="Manage saved session state.")
app.add_typer(session_app, name="session")


@session_app.command("show")
def session_show():
    data = _load_session()
    if not data:
        rprint(f"[yellow]No session saved yet.[/yellow] [dim](will be created at {SESSION_FILE})[/dim]")
        return
    table = Table(title=f"Session  [dim]{SESSION_FILE}[/dim]", border_style="bright_black", header_style="bold magenta")
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", style="green")
    for k, v in sorted(data.items()):
        table.add_row(k, str(v))
    console.print(table)


@session_app.command("set")
def session_set(
        team: Optional[int] = typer.Option(None, "--team", "-t", help="FRC team number"),
        host: Optional[str] = typer.Option(None, "--host", "-H", help="roboRIO hostname or IP"),
        path: Optional[str] = typer.Option(None, "--path", "-p", help="Default remote directory"),
):
    updates = {}
    if team is not None:
        updates["team"] = team
        updates["host"] = DEFAULT_HOST_TEMPLATE.format(team=team)
    if host is not None:
        updates["host"] = host
    if path is not None:
        updates["path"] = path
    if not updates:
        rprint("[yellow]Nothing to set — pass at least one option.[/yellow]")
        return
    _save_session(updates)
    rprint("[green]✓ Session updated.[/green]")
    session_show()


@session_app.command("clear")
def session_clear():
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        rprint(f"[green]✓ Session cleared.[/green]  [dim]({SESSION_FILE})[/dim]")
    else:
        rprint("[yellow]No session file found.[/yellow]")


def make_client(host: str, port: int, user: str, password: Optional[str] = "") -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=10,
        )
    except Exception as e:
        rprint(f"[bold red]✗ Connection failed:[/bold red] {e}")
        raise typer.Exit(code=1)
    return client


@app.command("ls")
def list_files(
        team: Optional[int] = typer.Argument(None, help="FRC team number (saved after first use)"),
        remote_path: Optional[str] = typer.Option(None, "--path", "-p", help="Remote path to list"),
        host: Optional[str] = typer.Option(None, "--host", "-H", help="Override hostname"),
        port: int = typer.Option(DEFAULT_PORT, "--port", help="SSH port"),
        user: str = typer.Option(DEFAULT_USER, "--user", "-u", help="SSH username"),
        password: Optional[str] = typer.Option("", "--password", "-P", help="SSH password", envvar="ROBORIO_PASSWORD"),
        all_files: bool = typer.Option(False, "--all", "-a", help="Include hidden files"),
):
    resolved_host, port, resolved_path = _resolved_connection(team, host, port, remote_path)
    console.print(f"[dim]Connecting to [bold]{resolved_host}[/bold]…[/dim]")
    client = make_client(resolved_host, port, user, password=password)

    with client:
        sftp = client.open_sftp()
        with sftp:
            try:
                entries = sftp.listdir_attr(resolved_path)
            except FileNotFoundError:
                rprint(f"[red]✗ Path not found:[/red] {resolved_path}")
                raise typer.Exit(1)

            entries.sort(key=lambda e: (not stat_mod.S_ISDIR(e.st_mode), e.filename.lower()))

            table = Table(
                title=f"[bold cyan]{resolved_path}[/bold cyan]  on  [bold]{resolved_host}[/bold]",
                show_header=True,
                header_style="bold magenta",
                border_style="bright_black",
                row_styles=["", "dim"],
            )
            table.add_column("Name", style="bold", no_wrap=True)
            table.add_column("Size", justify="right", style="cyan")
            table.add_column("Modified", style="green")
            table.add_column("Type", style="yellow")

            shown = 0
            for attr in entries:
                name = attr.filename
                if not all_files and name.startswith("."):
                    continue
                is_dir = stat_mod.S_ISDIR(attr.st_mode)
                is_link = stat_mod.S_ISLNK(attr.st_mode)
                size_str = "-" if is_dir else _human_size(attr.st_size)
                mtime = datetime.fromtimestamp(attr.st_mtime).strftime("%Y-%m-%d %H:%M")
                kind = "dir" if is_dir else ("link" if is_link else _file_kind(name))
                display_name = f"[bold blue]{name}/[/bold blue]" if is_dir else name
                table.add_row(display_name, size_str, mtime, kind)
                shown += 1

            console.print(table)
            console.print(f"[dim]{shown} item(s)[/dim]")


@app.command("get")
def download(
        team: Optional[int] = typer.Argument(None, help="FRC team number (saved after first use)"),
        remote_file: str = typer.Argument(..., help="Remote file path to download"),
        local_dest: Optional[str] = typer.Option(None, "--dest", "-d", help="Local destination path"),
        host: Optional[str] = typer.Option(None, "--host", "-H", help="Override hostname"),
        port: int = typer.Option(DEFAULT_PORT, "--port", help="SSH port"),
        user: str = typer.Option(DEFAULT_USER, "--user", "-u", help="SSH username"),
        password: Optional[str] = typer.Option("", "--password", "-P", help="SSH password", envvar="ROBORIO_PASSWORD"),
):
    resolved_host, port, _ = _resolved_connection(team, host, port, None)
    console.print(f"[dim]Connecting to [bold]{resolved_host}[/bold]…[/dim]")
    client = make_client(resolved_host, port, user, password=password)

    filename = PurePosixPath(remote_file).name
    local_path = Path(local_dest) if local_dest else Path.cwd() / filename

    with client:
        sftp = client.open_sftp()
        with sftp:
            _do_download(sftp, remote_file, local_path)

    rprint(f"[bold green]✓ Saved to {local_path}[/bold green]")


@app.command("get-logs")
def download_all_logs(
        team: Optional[int] = typer.Argument(None, help="FRC team number (saved after first use)"),
        remote_path: Optional[str] = typer.Option(None, "--path", "-p", help="Remote directory to search"),
        local_dest: Optional[str] = typer.Option(None, "--dest", "-d", help="Local destination directory"),
        host: Optional[str] = typer.Option(None, "--host", "-H", help="Override hostname"),
        port: int = typer.Option(DEFAULT_PORT, "--port", help="SSH port"),
        user: str = typer.Option(DEFAULT_USER, "--user", "-u", help="SSH username"),
        password: Optional[str] = typer.Option("", "--password", "-P", help="SSH password", envvar="ROBORIO_PASSWORD"),
):
    resolved_host, port, resolved_path = _resolved_connection(team, host, port, remote_path)
    console.print(f"[dim]Connecting to [bold]{resolved_host}[/bold]…[/dim]")
    client = make_client(resolved_host, port, user, password=password)

    dest_dir = Path(local_dest) if local_dest else Path.cwd()
    dest_dir.mkdir(parents=True, exist_ok=True)

    with client:
        sftp = client.open_sftp()
        with sftp:
            try:
                entries = sftp.listdir_attr(resolved_path)
            except FileNotFoundError:
                rprint(f"[red]✗ Path not found:[/red] {resolved_path}")
                raise typer.Exit(1)

            logs = [e for e in entries if e.filename.endswith(".wpilog")]
            if not logs:
                rprint(f"[yellow]No .wpilog files found in {resolved_path}[/yellow]")
                return

            rprint(f"[bold]Found {len(logs)} .wpilog file(s)[/bold]")
            for entry in logs:
                remote_file = f"{resolved_path.rstrip('/')}/{entry.filename}"
                local_path = dest_dir / entry.filename
                _do_download(sftp, remote_file, local_path, total=entry.st_size)
                rprint(f"  [green]✓[/green] {local_path}")


@app.command("getlatestsysid")
def get_latest_sysid(
        team: Optional[int] = typer.Argument(None, help="FRC team number (saved after first use)"),
        state_field: str = typer.Argument(
            ...,
            help=(
                    'DataLog entry name that holds the SysId state string, '
                    'e.g. "sysid-test-state-drive" or "/SmartDashboard/SysIdTestState"'
            ),
        ),
        remote_path: Optional[str] = typer.Option(None, "--path", "-p", help="Remote directory to search"),
        host: Optional[str] = typer.Option(None, "--host", "-H", help="Override hostname"),
        port: int = typer.Option(DEFAULT_PORT, "--port", help="SSH port"),
        user: str = typer.Option(DEFAULT_USER, "--user", "-u", help="SSH username"),
        password: Optional[str] = typer.Option("", "--password", "-P", help="SSH password", envvar="ROBORIO_PASSWORD"),
        local_dest: Optional[str] = typer.Option(None, "--dest", "-d", help="Download destination directory"),
):
    try:
        from rioextract import datalog as dl
    except ImportError:
        rprint(
            "[bold red]✗ datalog.py must be on the Python path alongside this script.[/bold red]\n"
            "[dim]Place the WPILib datalog.py in the same directory as roborio_sftp.py.[/dim]"
        )
        raise typer.Exit(1)

    resolved_host, port, resolved_path = _resolved_connection(team, host, port, remote_path)
    console.print(f"[dim]Connecting to [bold]{resolved_host}[/bold]…[/dim]")
    client = make_client(resolved_host, port, user, password=password)

    with client:
        sftp = client.open_sftp()
        with sftp:
            try:
                entries = sftp.listdir_attr(resolved_path)
            except FileNotFoundError:
                rprint(f"[red]✗ Path not found:[/red] {resolved_path}")
                raise typer.Exit(1)

            logs = sorted(
                [e for e in entries if e.filename.endswith(".wpilog")],
                key=lambda e: e.st_mtime,
                reverse=True,
            )

            if not logs:
                rprint(f"[yellow]No .wpilog files found in {resolved_path}[/yellow]")
                raise typer.Exit(1)

            rprint(
                f"[dim]Searching [bold]{len(logs)}[/bold] log(s) newest-first for "
                f"SysId field [bold cyan]{state_field!r}[/bold cyan]…[/dim]\n"
            )

            found_remote: Optional[str] = None
            found_attr = None

            for attr in logs:
                remote_file = f"{resolved_path.rstrip('/')}/{attr.filename}"
                mtime_str = datetime.fromtimestamp(attr.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

                console.print(
                    f"  [dim]Checking[/dim] [bold]{attr.filename}[/bold] "
                    f"[dim]({_human_size(attr.st_size)}, {mtime_str})[/dim] … ",
                    end="",
                )

                buf = io.BytesIO()
                try:
                    sftp.getfo(remote_file, buf)
                except Exception as exc:
                    console.print(f"[red]read error: {exc}[/red]")
                    continue

                raw = buf.getvalue()
                reader = dl.DataLogReader(raw)

                if not reader.isValid():
                    console.print("[yellow]not a valid wpilog, skipping[/yellow]")
                    continue

                states_seen = _collect_sysid_states(reader, state_field, dl)

                if states_seen >= SYSID_STATES:
                    console.print("[bold green]✓ complete SysId test found![/bold green]")
                    found_remote = remote_file
                    found_attr = attr
                    break
                else:
                    missing = SYSID_STATES - states_seen
                    if states_seen:
                        console.print(
                            f"[yellow]incomplete[/yellow] "
                            f"[dim](found: {', '.join(sorted(states_seen))} | "
                            f"missing: {', '.join(sorted(missing))})[/dim]"
                        )
                    else:
                        console.print(f"[dim]field {state_field!r} not present[/dim]")

            console.print()

            if found_remote is None:
                rprint(
                    "[bold red]✗ No log file containing a complete SysId test was found.[/bold red]\n"
                    f"[dim]Make sure the state field name [bold]{state_field!r}[/bold] "
                    "matches what your robot code writes to the DataLog.[/dim]"
                )
                raise typer.Exit(1)

            mtime_str = datetime.fromtimestamp(found_attr.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            console.print(
                Panel(
                    f"[bold green]{found_attr.filename}[/bold green]\n"
                    f"Size: [cyan]{_human_size(found_attr.st_size)}[/cyan]   "
                    f"Modified: [cyan]{mtime_str}[/cyan]\n"
                    f"Remote path: [dim]{found_remote}[/dim]",
                    title="Latest SysId Log",
                    border_style="green",
                )
            )

            do_download = typer.confirm("Download this file?", default=True)
            if not do_download:
                rprint("[dim]Skipping download.[/dim]")
                return

            dest_dir = Path(local_dest) if local_dest else Path.cwd()
            dest_dir.mkdir(parents=True, exist_ok=True)
            local_path = dest_dir / found_attr.filename

            _do_download(sftp, found_remote, local_path, total=found_attr.st_size)
            rprint(f"\n[bold green]✓ Saved to {local_path}[/bold green]")


def _collect_sysid_states(reader, state_field: str, dl) -> set:
    entries: dict = {}
    states_seen: set = set()

    for record in reader:
        if record.isStart():
            try:
                data = record.getStartData()
                entries[data.entry] = data
            except TypeError:
                pass
            continue

        if record.isControl():
            continue

        entry = entries.get(record.entry)
        if entry is None or entry.name != state_field:
            continue

        if entry.type not in ("string", "json"):
            continue

        try:
            value = record.getString().strip()
        except Exception:
            continue

        if value in SYSID_STATES:
            states_seen.add(value)
            if states_seen >= SYSID_STATES:
                return states_seen

    return states_seen


def _do_download(sftp, remote_file: str, local_path: Path, total: Optional[int] = None):
    if total is None:
        try:
            total = sftp.stat(remote_file).st_size
        except Exception:
            total = 0

    filename = PurePosixPath(remote_file).name
    transferred = [0]

    with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
    ) as progress:
        task = progress.add_task(filename, total=total or 1)

        def _callback(xferred: int, _total: int):
            delta = xferred - transferred[0]
            transferred[0] = xferred
            progress.update(task, advance=delta)

        sftp.get(remote_file, str(local_path), callback=_callback)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _file_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {
        ".wpilog": "wpilog",
        ".py": "python",
        ".json": "json",
        ".txt": "text",
        ".log": "log",
        ".jar": "jar",
    }.get(ext, ext.lstrip(".") or "file")


if __name__ == "__main__":
    app()
