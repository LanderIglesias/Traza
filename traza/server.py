"""Servidor local (design.md §5, §9, §10): solo 127.0.0.1, solo lectura, Host/Origin validados.

Invariante: el event loop nunca espera a SQLite. El watcher escribe desde su hilo
(`watcher.watch`); los endpoints de lectura son `def`, que FastAPI ejecuta en su pool de hilos,
cada uno con su conexión de solo lectura (WAL: los lectores no esperan al escritor).
"""
import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, views
from .watcher import watch

STATIC = Path(__file__).parent / "static"
HEARTBEAT_S = 15
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")


class Hub:
    """Reparte los ticks del watcher a cada navegador conectado. Cola acotada por cliente: si uno
    no lee, se descarta lo más viejo; publicar nunca bloquea al watcher (§10)."""

    def __init__(self, maxsize: int = 100):
        self.maxsize = maxsize
        self.queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(self.maxsize)
        self.queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.queues.discard(q)

    def publish(self, msg: dict | None) -> None:
        for q in self.queues:
            if q.full():
                q.get_nowait()
            q.put_nowait(msg)

    def close(self) -> None:
        """Al apagar: None dice a cada stream SSE que termine (si no, Ctrl+C espera a que el
        navegador cierre la pestaña)."""
        self.publish(None)


async def sse_stream(hub: Hub, requested_gen, current_gen, heartbeat: float, is_disconnected):
    """Eventos SSE para un navegador (design.md §10): hello, tick, ping (latido con nombre, que el
    cliente sí ve: un comentario ': ping' es invisible para EventSource) y reload."""
    if requested_gen is not None and requested_gen != current_gen:
        yield "event: reload\ndata: {}\n\n"  # la caché se reconstruyó: recargar entero
        return
    q = hub.subscribe()
    try:
        yield f"event: hello\ndata: {json.dumps({'gen': current_gen})}\n\n"
        while not await is_disconnected():
            try:
                msg = await asyncio.wait_for(q.get(), timeout=heartbeat)
            except TimeoutError:
                yield "event: ping\ndata: {}\n\n"
                continue
            if msg is None:  # el servidor se apaga
                return
            yield f"id: {msg['id']}\nevent: tick\ndata: {json.dumps(msg)}\n\n"
    finally:
        hub.unsubscribe(q)


def create_app(db_path, root, port: int, interval: float = 0.5,
               heartbeat: float = HEARTBEAT_S) -> FastAPI:
    db_path = Path(db_path)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    hub = Hub()
    state = {"generation": None, "tick": 0}

    def reader() -> sqlite3.Connection:
        return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)

    async def on_tick(stats: dict) -> None:
        if stats["files_read"] or stats["deleted"]:  # solo si algo cambió
            state["tick"] += 1
            hub.publish({"id": state["tick"], "gen": state["generation"]})

    @asynccontextmanager
    async def lifespan(app):
        def generation() -> str:          # crea/valida la caché, todo en el mismo hilo
            with closing(db.connect(db_path)) as conn:
                return db.generation(conn)
        state["generation"] = await asyncio.to_thread(generation)
        task = asyncio.create_task(watch(db_path, root, interval, on_tick))
        yield
        hub.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)  # espera a que suelte su conexión

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # DNS rebinding: solo se atiende a quien nos llama por nuestro nombre local
        if request.headers.get("host") not in allowed_hosts:
            return PlainTextResponse("bad host", status_code=400)
        # Otra web abierta en el navegador no puede leer el panel (Origin, si viene, es el nuestro)
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return PlainTextResponse("bad origin", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/overview")
    def overview():
        with closing(reader()) as conn:
            return views.overview(conn)

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str):
        with closing(reader()) as conn:
            s = views.session_summary(conn, session_id)
        if s is None:
            raise HTTPException(404)
        return s

    @app.get("/events")
    async def events(request: Request, gen: str | None = None):
        stream = sse_stream(hub, gen, state["generation"], heartbeat, request.is_disconnected)
        return StreamingResponse(stream, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
