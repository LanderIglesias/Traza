"""traza serve | scan | report — punto de entrada (`python -m traza` o `traza`)."""
import argparse
import re
import socket
import sys
import threading
import time
import urllib.request
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
    serve.add_argument("--allow-frame", type=origin, metavar="ORIGIN",
                       help="let this origin embed the panel in an iframe (e.g. agent-hub)")
    sub.add_parser("scan", help="build/refresh the cache and print what it holds")
    sub.add_parser("report", help="tokens and estimated cost of one session file")
    args = ap.parse_args(argv)

    # Puerto ocupado: error claro, y no abrir el navegador contra otro programa. El socket
    # comprobado es el que sirve: soltarlo dejaba una ventana para que otro proceso lo ocupara
    # antes que uvicorn (auditoría F7). En Windows, SO_REUSEADDR permitiría robarlo: exclusivo.
    sock = socket.socket()
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    sock.setsockopt(socket.SOL_SOCKET, exclusive or socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", args.port))
    except OSError:
        sock.close()
        print(f"port {args.port} is in use; try: traza serve --port {args.port + 1}",
              file=sys.stderr)
        return 1

    from .server import create_app, make_server

    url = f"http://127.0.0.1:{args.port}"
    print(f"traza on {url}  (cache: {args.db}, reading: {args.root})")
    if not args.no_open:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    app = create_app(args.db, args.root, port=args.port, frame_origin=args.allow_frame)
    make_server(app, args.port).run(sockets=[sock])
    return 0


def origin(value: str) -> str:
    """Un origen y nada más (esquema://host[:puerto], puerto "*" = cualquiera: la ventana de
    agent-hub cambia de puerto en cada arranque). El valor va dentro de la cabecera CSP, y un ";"
    o un espacio colarían directivas nuevas."""
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:([0-9]{1,5}|\*))?", value):
        raise argparse.ArgumentTypeError(f"not an origin like http://127.0.0.1:8090: {value!r}")
    return value


def _open_when_ready(url: str, tries: int = 50) -> None:
    """Abre el navegador solo cuando el panel responde: si el servidor no llega a arrancar
    (caché ajena, puerto perdido), no se abre una pestaña contra nada."""
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url + "/api/overview", timeout=1) as r:
                if r.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            pass
        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
