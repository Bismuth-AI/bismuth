"""The command line."""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import urlparse

import anyio
import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from bismuth import __version__
from bismuth.adapters.llm import list_models, litellm_adapter
from bismuth.adapters.parsers import build_registry
from bismuth.config import CONFIG_FILE, Settings, load_env_file
from bismuth.container import Bismuth, build
from bismuth.domain.errors import BismuthError
from bismuth.logging_setup import configure_logging
from bismuth.ports.vault import INBOX


def _force_utf8_output() -> None:
    """Force UTF-8 stdout/stderr so a Korean Windows console (cp949) doesn't crash on non-ASCII output."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # Redirected or unusual streams may not take it; never worth dying over.
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


_force_utf8_output()

app = typer.Typer(
    name="bismuth",
    help="에이전트가 탐색할 수 있는 문서 구조를, 알아서. 인자 없이 실행하면 시작됩니다.",
    # Bare `bismuth` starts the app instead of printing the subcommand list.
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
    # We render our own failures instead of Typer's provider-internals traceback.
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True, style="red")

_verbose = False


def _engine(vault: Path | None = None) -> Bismuth:
    settings = Settings()
    if vault is not None:
        settings = settings.model_copy(update={"vault_path": vault.expanduser().resolve()})
    engine = build(settings)
    # Every entry point recovers first, so a crash mid-batch is never left for the user to notice.
    if recovered := engine.recover():
        console.print(f"[yellow]이전 실행에서 중단된 변경 {recovered}건을 되돌렸습니다.")
    return engine


VaultOption = Annotated[
    Path | None,
    typer.Option("--vault", "-v", help="볼트 폴더. 기본값은 설정에 저장된 경로입니다."),
]


@app.callback()
def _root(
    context: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="디버그 로그와 전체 트레이스백을 봅니다.")
    ] = False,
) -> None:
    global _verbose
    _verbose = verbose
    # Must run before Settings() is constructed.
    load_env_file()
    configure_logging(verbose=verbose)
    if context.invoked_subcommand is None:
        serve()


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"bismuth {__version__}")


@app.command()
def doctor(vault: VaultOption = None) -> None:
    """Report what's configured and whether it actually responds."""
    settings = Settings()
    if vault is not None:
        settings = settings.model_copy(update={"vault_path": vault.expanduser().resolve()})

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("볼트", str(settings.vault_path))
    table.add_row("설정 파일", str(CONFIG_FILE) if CONFIG_FILE.exists() else "[dim]아직 설정 전[/]")
    table.add_row("", "")

    if not settings.is_configured:
        table.add_row("모델", "[yellow]설정되지 않음[/]")
        console.print(table)
        console.print("\n[cyan]bismuth[/] 를 실행하고 브라우저에서 설정을 마쳐 주세요.")
        return

    chosen = settings.provider
    assert chosen is not None
    table.add_row("프로바이더", chosen.label)
    table.add_row("모델", settings.model_for())
    table.add_row("엔드포인트", settings.api_base or "(프로바이더 기본값)")
    table.add_row(
        "API 키", f"…{settings.api_key[-4:]}" if settings.api_key else "[dim](필요 없음)[/]"
    )
    table.add_row(
        "데이터가 이 컴퓨터를 벗어나는가",
        "[green]아니오 — 완전 로컬[/]"
        if settings.runs_locally
        else "[yellow]예 — 외부 모델을 씁니다[/]",
    )
    table.add_row("", "")
    formats = build_registry().supported_extensions()
    table.add_row("읽을 수 있는 형식", " ".join(sorted(formats)))
    console.print(table)

    if ".hwpx" in formats:
        # See docs/licensing.md for why we read .hwpx ourselves and not the AGPL .hwp library.
        console.print(
            "\n[dim]참고: 한글 문서는 .hwpx 로 읽습니다. 구형 바이너리 .hwp 는 "
            "한글에서 .hwpx 로 다시 저장한 뒤 넣어 주세요.[/]"
        )

    with console.status("프로바이더에 무엇을 쓸 수 있는지 묻는 중..."):
        check = list_models(chosen.id, api_key=settings.api_key, api_base=settings.api_base)

    if not check.ok:
        console.print(f"\n[red]프로바이더가 응답하지 않았습니다:[/] {check.error}")
        console.print("\n[cyan]bismuth[/] 를 실행해 설정 화면에서 고쳐 주세요.")
        raise typer.Exit(1)

    console.print(
        f"\n[green]프로바이더가 응답했습니다.[/] 이 키로 쓸 수 있는 모델 {len(check.models)}개."
    )
    if settings.model not in check.models:
        console.print(
            f"  [yellow]![/] 모델 [cyan]{settings.model}[/] 이(가) 그 목록에 없습니다. "
            f"호출하면 실패합니다. [cyan]bismuth[/] 를 실행해 다른 걸 골라 주세요."
        )


@app.command()
def add(
    files: Annotated[list[Path], typer.Argument(help="추가할 파일. 볼트로 복사됩니다.")],
    vault: VaultOption = None,
) -> None:
    """Copy files into the vault and file them."""
    engine = _engine(vault)
    anyio.run(_add, engine, files)


async def _add(engine: Bismuth, files: list[Path]) -> None:
    try:
        for path in files:
            if not path.is_file():
                error_console.print(f"{path} 건너뜀: 파일이 아닙니다")
                continue
            rel = engine.ingest.stage(path.read_bytes(), path.name)
            await _process_one(engine, rel)
    finally:
        await litellm_adapter.close_clients()


@app.command()
def scan(vault: VaultOption = None) -> None:
    """Read and file everything unprocessed in the inbox, including hand-dropped files."""
    engine = _engine(vault)
    pending = engine.ingest.pending_inbox()
    if not pending:
        console.print("[green]기다리는 파일이 없습니다.")
        return
    console.print(f"문서 {len(pending)}개를 읽는 중...\n")
    anyio.run(_scan, engine, pending)


async def _scan(engine: Bismuth, pending: list[PurePosixPath]) -> None:
    try:
        for rel in pending:
            await _process_one(engine, rel)
    finally:
        await litellm_adapter.close_clients()


async def _process_one(engine: Bismuth, rel: PurePosixPath) -> None:
    try:
        result = await engine.ingest.process(rel)
    except BismuthError as exc:
        error_console.print(f"  {rel}: {exc}")
        return

    if result.duplicate:
        console.print(f"  [dim]{result.filename} — 이미 읽은 문서라 그대로 뒀습니다[/]")
        return

    if result.placement.is_placed:
        badge = "[dim](새 폴더)[/]" if result.placement.created_folder else ""
        console.print(f"  [green]→[/] {result.filename}  →  [cyan]{result.destination}/[/] {badge}")
        console.print(f"    [dim]{result.placement.rationale}[/]")
    else:
        console.print(f"  [yellow]?[/] {result.filename}  →  [yellow]{INBOX}/[/]")
        console.print(f"    [dim]{result.placement.rationale}[/]")


@app.command()
def tree(vault: VaultOption = None) -> None:
    """Show the vault as an agent sees it: folders and their purpose."""
    engine = _engine(vault)
    root = Tree(f"[bold]{engine.vault.root.name}[/]")
    nodes: dict[tuple[str, ...], Tree] = {(): root}

    for folder in engine.vault.iter_folders():
        if not folder.parts:
            continue
        parent = nodes.get(folder.parts[:-1], root)
        count = engine.vault.count_files(folder)
        label = f"[cyan]{folder.name}/[/]" + (f" [dim]({count})[/]" if count else "")
        try:
            if charter := engine.charters.load(folder):
                label += f"\n[dim]{charter.purpose}[/]"
        except BismuthError:
            label += " [red](헌장을 읽을 수 없음)[/]"
        nodes[folder.parts] = parent.add(label)

    console.print(root)


@app.command()
def status(vault: VaultOption = None) -> None:
    """Vault status: document count, folder count, what's left in the inbox."""
    engine = _engine(vault)
    total = engine.catalog.card_count()
    folders = sum(
        1 for f in engine.vault.iter_folders() if f.parts and f.parts[0] != INBOX.parts[0]
    )
    inbox = engine.vault.count_files(INBOX, recursive=True)

    console.print(f"[bold]{engine.vault.root}[/]\n")
    console.print(f"  문서    {total}")
    console.print(f"  폴더    {folders}")
    console.print(f"  인박스  {inbox}" + ("  [yellow](읽었지만 배치 못 함)[/]" if inbox else ""))


@app.command(name="log")
def show_log(
    vault: VaultOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
) -> None:
    """Everything Bismuth has done to this vault."""
    engine = _engine(vault)
    table = Table(box=None, padding=(0, 2))
    table.add_column("id", style="dim")
    table.add_column("언제", style="dim")
    table.add_column("누가")
    table.add_column("무엇을")
    table.add_column("상태")

    for entry in engine.journal.iter_entries(limit=limit):
        colour = {"applied": "green", "reverted": "dim", "failed": "red"}.get(
            entry.status.value, "yellow"
        )
        table.add_row(
            entry.id,
            entry.created_at.strftime("%m-%d %H:%M"),
            entry.actor.value,
            entry.reason,
            f"[{colour}]{entry.status.value}[/]",
        )
    console.print(table)
    console.print("\n[dim]되돌리려면: bismuth undo <id>[/]")


@app.command()
def undo(
    entry_id: Annotated[str, typer.Argument(help="저널 항목 id. 'bismuth log' 에서 확인합니다.")],
    vault: VaultOption = None,
) -> None:
    """Reverse a change. Any change."""
    engine = _engine(vault)
    try:
        reversed_entry = engine.transactor.undo(entry_id)
    except BismuthError as exc:
        error_console.print(str(exc))
        raise typer.Exit(1) from exc
    console.print(f"[green]되돌렸습니다.[/] {reversed_entry.reason}")
    console.print(
        f"[dim]이 되돌리기 자체가 항목 {reversed_entry.id} 이며, 다시 되돌릴 수 있습니다.[/]"
    )


@app.command()
def serve(
    vault: VaultOption = None,
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port")] = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="서버가 뜨면 브라우저를 엽니다.")
    ] = True,
) -> None:
    """Run Bismuth. What `bismuth` with no subcommand does."""
    try:
        import uvicorn
    except ImportError as exc:
        error_console.print("서버에는 추가 패키지가 필요합니다: pip install 'bismuth-kb[server]'")
        raise typer.Exit(1) from exc

    settings = Settings()
    if vault is not None:
        settings = settings.model_copy(update={"vault_path": vault.expanduser().resolve()})

    from bismuth.api.app import create_app

    bound_host = host or settings.host
    bound_port = port or settings.port
    url = f"http://{bound_host}:{bound_port}"

    console.print(f"\n  [green]Bismuth[/] → [cyan]{url}[/]")
    if settings.is_configured:
        console.print(f"  [dim]볼트: {settings.vault_path}[/]\n")
    else:
        console.print("  [dim]아직 설정 전 — 열리는 페이지가 안내합니다[/]\n")

    if open_browser:
        _open_when_ready(url)

    uvicorn.run(
        create_app(settings, verbose=_verbose),
        host=bound_host,
        port=bound_port,
        log_level="warning",
    )


def _open_when_ready(url: str, *, timeout: float = 10.0) -> None:
    """Open a browser once the port answers, polled from a background thread (a cold LiteLLM import can take seconds)."""
    parsed = urlparse(url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80

    def wait_and_open() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.3):
                    webbrowser.open(url)
                    return
            except OSError:
                time.sleep(0.15)

    threading.Thread(target=wait_and_open, daemon=True).start()


def main() -> None:
    """Entry point. Turns provider exceptions into a message, a hint, and ``--verbose`` for the traceback."""
    try:
        app()
    except BismuthError as exc:
        # Ours, and deliberate. The message is already written for a human.
        error_console.print(f"\n{exc}")
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        error_console.print("\n중단했습니다. 하다 만 변경은 다음 실행 때 되돌려집니다.")
        raise SystemExit(130) from None
    except Exception as exc:
        if _verbose:
            raise
        error_console.print(f"\n{type(exc).__name__}: {exc}")
        if hint := _hint_for(exc):
            error_console.print(f"\n{hint}")
        error_console.print("\n[dim]전체 트레이스백을 보려면 --verbose 를 붙이세요.[/]")
        raise SystemExit(1) from exc


def _hint_for(exc: Exception) -> str:
    """Guess at the fix for the failures that are not our fault but are our problem."""
    text = f"{type(exc).__name__} {exc}".lower()
    if "api key" in text or "not active" in text or "authentication" in text:
        return (
            "Bismuth 문제가 아니라 모델 인증 문제로 보입니다. `bismuth` 를 실행해 설정 화면에서 "
            "키를 다시 넣거나, Ollama 같은 로컬 모델을 고르면 키 자체가 필요 없어집니다."
        )
    if "rate limit" in text or "quota" in text:
        return "프로바이더가 요청 속도를 제한하고 있습니다. 잠시 뒤 다시 시도해 주세요."
    if "connection" in text or "connect" in text or "timeout" in text:
        return (
            "모델 엔드포인트에 연결하지 못했습니다. 로컬 모델이라면 실행 중인지, "
            "설정한 주소가 맞는지 확인해 주세요."
        )
    return ""


if __name__ == "__main__":
    main()
