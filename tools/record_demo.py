"""Graba el GIF del README con datos de demo (tools/make_demo_data.py), nunca con ~/.claude.

    python tools/record_demo.py                 # → docs/demo.gif y docs/screenshot.png

Una idea en ~12 s: en una sesión en vivo, el principal lanza dos subagentes; aparecen en el árbol
(ordenado por valor) trabajando y suben mientras gastan, terminan, y el Flame enseña quién se
llevó el valor. Necesita `pip install -e .[demo]`, `playwright install chromium` y ffmpeg:
herramientas de grabación, no dependencias de traza (pipx install no las descarga).
"""
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import make_demo_data as demo  # noqa: E402

PORT = 7433
URL = f"http://127.0.0.1:{PORT}"
OUT = Path(__file__).parent.parent / "docs" / "demo.gif"
SHOT = OUT.with_name("screenshot.png")   # la captura estática del README: el árbol al terminar


def wait_ready(url: str) -> None:
    for _ in range(100):
        try:
            with urllib.request.urlopen(url + "/api/overview", timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit("traza no arrancó")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="traza-demo-"))
    session = demo.build(tmp / "data")
    server = subprocess.Popen([sys.executable, "-m", "traza", "serve", "--no-open", "--port", str(PORT),
                               "--root", str(tmp / "data" / "projects"), "--db", str(tmp / "data" / "traza.db")])
    try:
        wait_ready(URL)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 960}, color_scheme="light",
                                      record_video_dir=str(tmp / "video"),
                                      record_video_size={"width": 1280, "height": 960})
            page = ctx.new_page()
            t0 = time.monotonic()
            page.goto(URL)
            page.click(f'[data-id="{session.sid}"]')
            page.wait_for_selector("#tree-rows tr")
            # ordenado por valor propio: los subagentes nuevos salen abajo y suben mientras gastan,
            # con las cabeceras de la tabla a la vista
            page.click('.tree th[data-sort="cost"] button')
            page.wait_for_timeout(1200)
            start = time.monotonic() - t0                  # aquí empieza el GIF
            runner = threading.Thread(target=demo.live, args=(session,))
            runner.start()
            runner.join()
            page.wait_for_timeout(1500)                    # terminan, con su coste
            SHOT.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(SHOT))
            page.click('.tabs [data-tab="flame"]')
            page.wait_for_timeout(3500)
            end = time.monotonic() - t0
            video = page.video.path()
            ctx.close()
            browser.close()
        OUT.parent.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
                        "-i", video, "-vf", "fps=12,scale=1000:-1:flags=lanczos,split[a][b];"
                        "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
                        str(OUT)], check=True)
        print(f"{OUT}  ·  {end - start:.1f} s  ·  {OUT.stat().st_size / 1e6:.1f} MB")
    finally:
        server.terminate()
        server.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)      # los datos de demo no se quedan en disco


if __name__ == "__main__":
    main()
