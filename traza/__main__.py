"""traza serve | scan | report — punto de entrada (`python -m traza` o `traza`)."""
import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

DEFAULT_DB = Path.home() / ".traza" / "traza.db"
DEFAULT_ROOT = Path.home() / ".claude" / "projects"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["scan"]:
        from .scan import main as scan_main
        return scan_main(argv[1:])
    if argv[:1] == ["report"]:
        from .report import main as report_main
        return report_main(argv[1:])

    ap = argparse.ArgumentParser(prog="traza", description="Local live panel for Claude Code.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="start the panel on 127.0.0.1")
    serve.add_argument("--port", type=int, default=7420)
    serve.add_argument("--db", type=Path, default=DEFAULT_DB)
    serve.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    serve.add_argument("--no-open", action="store_true", help="do not open the browser")
    sub.add_parser("scan", help="build/refresh the cache and print what it holds")
    sub.add_parser("report", help="tokens and estimated cost of one session file")
    args = ap.parse_args(argv)

    # Puerto ocupado: error claro, y no abrir el navegador contra otro programa.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            print(f"port {args.port} is in use; try: traza serve --port {args.port + 1}",
                  file=sys.stderr)
            return 1

    import uvicorn
    from .server import create_app

    url = f"http://127.0.0.1:{args.port}"
    print(f"traza on {url}  (cache: {args.db}, reading: {args.root})")
    if not args.no_open:  # cuando el servidor ya escucha, no antes
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    # Solo 127.0.0.1: el panel nunca escucha en la red (design.md §9).
    uvicorn.run(create_app(args.db, args.root, port=args.port), host="127.0.0.1",
                port=args.port, log_level="warning", timeout_graceful_shutdown=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
