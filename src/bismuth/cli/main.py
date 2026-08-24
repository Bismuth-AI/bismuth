"""Launch the local Bismuth web application."""

from __future__ import annotations

import argparse
import contextlib
import socket
import sys
import threading
import time
import webbrowser
from asyncio import AbstractEventLoop, SelectorEventLoop
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bismuth import __version__
from bismuth.config import Settings, load_env_file
from bismuth.domain.errors import BismuthError
from bismuth.logging_setup import configure_logging


def _force_utf8_output() -> None:
    """Use UTF-8 for Windows console output."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bismuth",
        description="Launch the local Bismuth web app.",
    )
    parser.add_argument(
        "--vault",
        "-v",
        type=Path,
        help="Vault directory. Uses the saved setting when omitted.",
    )
    parser.add_argument("--host", help="Server address. Only localhost is allowed.")
    parser.add_argument("--port", type=_port, help="Server port.")
    parser.add_argument(
        "--open",
        dest="open_browser",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Open a browser when the server is ready.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show full error details.")
    parser.add_argument("--version", action="version", version=f"bismuth {__version__}")
    return parser


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be a number") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _serve(args: argparse.Namespace) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "server dependencies are missing: pip install 'bismuth-kb[server]'"
        ) from exc

    from bismuth.api.app import create_app

    settings = Settings()
    if args.vault is not None:
        settings = settings.model_copy(update={"vault_path": args.vault.expanduser().resolve()})

    host = args.host or settings.host
    port = args.port or settings.port
    if not _is_loopback_host(host):
        raise ValueError("Bismuth can only serve on localhost.")

    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    url = f"http://{url_host}:{port}"
    print(f"\n  Bismuth → {url}")
    if settings.is_configured:
        print(f"  Vault: {settings.vault_path}\n")
    else:
        print("  Setup required — finish configuration in the browser.\n")

    if args.open_browser:
        _open_when_ready(url)

    uvicorn.run(
        create_app(settings, verbose=args.verbose),
        host=host,
        port=port,
        log_level="warning",
        **_loop_choice(),
    )


def _is_loopback_host(host: str) -> bool:
    """Return whether a server host is restricted to this machine."""
    return host.strip().strip("[]").casefold() in {"localhost", "127.0.0.1", "::1"}


def _selector_loop() -> AbstractEventLoop:
    """Return the Windows event loop used by Uvicorn."""
    return SelectorEventLoop()


def _loop_choice() -> dict[str, Any]:
    """Use the selector loop on Windows for incremental streaming."""
    if sys.platform != "win32":
        return {}
    return {"loop": "bismuth.cli.main:_selector_loop"}


def _open_when_ready(url: str, *, timeout: float = 10.0) -> None:
    """Open the browser after the server starts accepting connections."""
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


def _hint_for(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if "api key" in text or "not active" in text or "authentication" in text:
        return "Check the API key and provider in Settings."
    if "rate limit" in text or "quota" in text:
        return "The provider rate limit was reached. Try again shortly."
    if "connection" in text or "connect" in text or "timeout" in text:
        return "Check that the model endpoint is running and its address is correct."
    return ""


def main(argv: Sequence[str] | None = None) -> None:
    """Run the local web application."""
    _force_utf8_output()
    args = _parser().parse_args(argv)
    try:
        load_env_file()
        configure_logging(verbose=args.verbose)
        _serve(args)
    except BismuthError as exc:
        print(f"\n{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("\nStopped. Incomplete changes will be recovered on the next run.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        if args.verbose:
            raise
        print(f"\n{type(exc).__name__}: {exc}", file=sys.stderr)
        if hint := _hint_for(exc):
            print(f"\n{hint}", file=sys.stderr)
        print("\nRun with --verbose to see the full error.", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
