"""F3: frontera HTTP (design.md §9, §10). Solo 127.0.0.1, Host/Origin validados, SSE."""
import asyncio
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traza import server

FIX = Path(__file__).parent / "fixtures"
STATIC = Path(server.__file__).parent / "static"
PORT = 7420
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIX, root / "proj")
    app = server.create_app(tmp_path / "traza.db", root, port=PORT, interval=0.05)
    with TestClient(app, base_url=BASE) as c:   # `with` arranca el watcher (lifespan)
        for _ in range(100):                     # espera al primer tick
            if c.get("/api/overview").json()["sessions"]:
                break
            asyncio.run(asyncio.sleep(0.02))
        yield c


# --- DNS rebinding / CSRF ---------------------------------------------------------------------

def test_host_ajeno_se_rechaza(client):
    assert client.get("/api/overview", headers={"host": "evil.com"}).status_code == 400
    assert client.get("/", headers={"host": f"evil.com:{PORT}"}).status_code == 400


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}"])
def test_host_local_se_acepta(client, host):
    assert client.get("/api/overview", headers={"host": host}).status_code == 200


def test_origin_ajeno_se_rechaza_y_ausente_se_acepta(client):
    assert client.get("/api/overview", headers={"origin": "http://evil.com"}).status_code == 403
    assert client.get("/api/overview", headers={"origin": BASE}).status_code == 200
    assert client.get("/api/overview").status_code == 200


def test_cabeceras_de_seguridad(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"


# --- API ---------------------------------------------------------------------------------------

def test_overview_y_resumen(client):
    ov = client.get("/api/overview").json()
    assert {s["id"] for s in ov["sessions"]} == {"sess-A", "sess-B"}
    s = client.get("/api/sessions/sess-A").json()
    assert s["title"] == "Listar ficheros"
    assert client.get("/api/sessions/no-existe").status_code == 404


def test_la_pagina_se_sirve(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>traza</title>" in r.text
    assert client.get("/static/app.js").status_code == 200


# --- SSE ---------------------------------------------------------------------------------------

def test_sse_con_otra_generacion_manda_recargar(client):
    with client.stream("GET", "/events?gen=vieja") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "event: reload" in r.read().decode()


def test_hub_reparte_y_descarta_lo_mas_viejo_si_un_cliente_no_lee():
    async def main():
        hub = server.Hub(maxsize=2)
        q = hub.subscribe()
        for i in range(5):
            hub.publish({"id": i})              # nunca bloquea al watcher
        got = [q.get_nowait()["id"], q.get_nowait()["id"]]
        hub.unsubscribe(q)
        return got
    assert asyncio.run(main()) == [3, 4]


# --- frontend: 100 % local y sin XSS ------------------------------------------------------------

def test_el_frontend_no_pide_nada_fuera_de_la_maquina():
    for f in STATIC.rglob("*"):
        if f.suffix in (".html", ".css", ".js"):
            # el espacio de nombres SVG es un identificador del estándar, no una petición
            text = f.read_text(encoding="utf-8").replace('"http://www.w3.org/2000/svg"', "")
            assert not re.search(r"(https?:)?//[a-z0-9.-]+\.[a-z]{2,}", text), f.name


def test_el_frontend_nunca_inyecta_html():
    for f in STATIC.rglob("*.js"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", text), f.name


def test_cerrar_el_hub_termina_los_streams():
    # Sin esto, Ctrl+C se queda esperando a que el navegador cierre la pestaña.
    async def main():
        hub = server.Hub()
        q = hub.subscribe()
        hub.close()
        return await asyncio.wait_for(q.get(), 1)
    assert asyncio.run(main()) is None


def test_serve_con_el_puerto_ocupado_no_abre_nada(capsys):
    import socket
    from traza.__main__ import main
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert main(["serve", "--no-open", "--port", str(port)]) == 1
    assert "in use" in capsys.readouterr().err


def test_sse_latido_tick_y_cierre():
    # EventSource no expone los comentarios (": ping"): el latido tiene que ser un evento con
    # nombre para que el cliente detecte una conexión muerta (design.md §10). Se prueba el
    # generador directamente: el TestClient no avisa de la desconexión de un stream infinito.
    async def main():
        hub = server.Hub()
        stream = server.sse_stream(hub, "g1", "g1", 0.05, lambda: asyncio.sleep(0, False))
        first = await asyncio.wait_for(anext(stream), 1)
        ping = await asyncio.wait_for(anext(stream), 1)      # nada que contar: latido
        hub.publish({"id": 7, "gen": "g1"})
        tick = await asyncio.wait_for(anext(stream), 1)
        hub.close()                                           # el servidor se apaga
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 1)
        return first, ping, tick, len(hub.queues)
    first, ping, tick, left = asyncio.run(main())
    assert first.startswith("event: hello")
    assert ping == "event: ping\ndata: {}\n\n"
    assert tick.startswith("id: 7\nevent: tick")
    assert left == 0                                          # se desuscribió al terminar


def test_apagar_con_una_pestana_abierta_no_espera_al_timeout(tmp_path):
    # uvicorn espera a que terminen las conexiones ANTES del shutdown del lifespan: los streams
    # SSE tienen que cerrarse al recibir la señal, no después (si no: 3 s y un ERROR en consola).
    import signal
    import threading
    import time
    import httpx
    root = tmp_path / "projects"
    root.mkdir()
    port = 7431
    srv = server.make_server(server.create_app(tmp_path / "t.db", root, port=port), port)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    got_hello = threading.Event()

    def listen():
        with httpx.stream("GET", f"http://127.0.0.1:{port}/events", timeout=10) as r:
            for line in r.iter_lines():
                if line.startswith("event: hello"):
                    got_hello.set()
    threading.Thread(target=listen, daemon=True).start()
    assert got_hello.wait(5)
    t0 = time.perf_counter()
    srv.handle_exit(signal.SIGINT, None)
    th.join(10)
    assert not th.is_alive() and time.perf_counter() - t0 < 2.0


def test_cada_peticion_http_lee_una_foto_coherente(tmp_path):
    # Un endpoint hace varias consultas; si el watcher hace commit entre dos, sin transacción la
    # segunda ve filas que la primera no vio (KeyError en vivo en agent_tree → 500 → el panel
    # se vaciaba). Con una transacción de lectura (WAL) todas ven la misma foto.
    from traza import db
    path = tmp_path / "t.db"
    writer = db.connect(path)
    with server.reader(path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        with writer:
            writer.execute("INSERT INTO sessions VALUES ('nueva', 'p', NULL)")
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == before
    writer.close()


# --- F5: vista de juicio y contenido bajo demanda ------------------------------------------------

def test_vista_de_agente_por_http(client):
    r = client.get("/api/sessions/sess-A/agents/abc")
    assert r.status_code == 200 and r.json()["agent"]["type"] == "Explore"
    assert client.get("/api/sessions/sess-A/agents/no-existe").status_code == 404
    assert client.get("/api/sessions/sess-A/agents/main?before=2").json()["items"] is not None


def test_contenido_es_json_con_el_texto_literal(client):
    ids = [str(e["id"]) for i in client.get("/api/sessions/sess-A/agents/main").json()["items"]
           if i["kind"] == "prompt" for e in [i["event"]]]
    r = client.get(f"/api/content?ids={','.join(ids)}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"   # nunca se interpreta como HTML
    assert r.json()[ids[0]]["text"] == "Hola, lista los ficheros"


@pytest.mark.parametrize("ids", ["", "abc", "1,,2", "1;DROP", "-1", ",".join(["1"] * 201)],
                         ids=["vacio", "texto", "hueco", "inyeccion", "negativo", "201-ids"])
def test_ids_de_contenido_se_validan(client, ids):
    assert client.get(f"/api/content?ids={ids}").status_code == 400


def test_pagina_y_estaticos_se_revalidan_siempre(client):
    # Sin Cache-Control, Chrome reutilizó un index.html viejo (caché heurística) con un app.js
    # nuevo: la página se rompió tras actualizar traza (visto en F6). no-cache = revalidar con
    # el ETag en cada carga, gratis en local.
    for path in ("/", "/static/app.js", "/static/styles.css"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
