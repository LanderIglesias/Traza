# traza — Plan por fases

> Estado: **borrador pendiente de aprobación** · Fecha: 2026-09-29 · Diseño: [`design.md`](design.md)

Reglas del plan:
- Cada fase termina con algo **visible y verificable** por el autor, no solo "compila".
- TDD: el test se escribe antes que el código que lo hace pasar.
- Cada cambio (fichero, dependencia, función) se explica en español al hacerlo.
- Antes de empezar cada fase se detalla en tareas pequeñas; este documento fija el orden y los
  criterios de "hecho".
- Si una fase descubre algo que contradice `design.md`, se para y se actualiza el diseño primero.

---

## F0 — Verificaciones previas (spike, ~medio día)

Resolver los **[sin verificar]** del diseño con pruebas reales. Todo el código de esta fase es
**desechable** y no entra en el repo.

| Pregunta | Cómo se comprueba |
|---|---|
| ¿Las líneas `assistant` se escriben al terminar la respuesta o en streaming? | comparar marcas de tiempo de las líneas de un mismo `requestId` en una sesión grabada a propósito |
| ¿`claude --resume` escribe en el mismo fichero o crea otro? | reanudar una sesión de prueba y observar `~/.claude/projects/` |
| ¿Por qué no cuadra el oráculo en `3416476c`? | inspeccionar dónde aparece su `cost-state` y si hay otro fichero con ese `sessionId` o con `claude-opus-5-5` |
| ¿Está libre el nombre `traza` en PyPI y GitHub? | búsqueda en pypi.org y github.com |
| Precios actuales de los modelos presentes en disco | referencia oficial de precios de Anthropic |

**Hecho cuando:** `docs/findings.md` responde cada pregunta con la evidencia (o "no se pudo
determinar" y su consecuencia), y `design.md` está actualizado si algo cambió.

## F1 — Parser puro + cálculo de tokens (~1 día)

Crear el repo (`git init`, `.gitignore` que excluya `.claude/`, `.serena/`, `*.db` y cualquier
JSONL real), `pyproject.toml`, `traza/parser.py`, `traza/pricing.py`, `prices.toml`, tests.

- Fixtures **sintéticas y sanitizadas** que reproducen los casos reales: petición multilínea con
  `usage` repetido, `<synthetic>`, `assistant` sin `requestId`, `tool_use`/`tool_result`,
  `api_error`, `compact_boundary`, tipo desconocido, subagente con `meta.json`.
- Comando de verificación: `python -m traza.report <fichero.jsonl>` imprime agentes, peticiones y
  tokens por modelo.

**Hecho cuando:**
- Tests verdes: deduplicación por `requestId`, cada tipo de línea, `NULL` vs 0, `<synthetic>` = 0,
  modelo desconocido = `?`, alias solo de sufijo de fecha.
- **Test oráculo:** en las sesiones de este disco con `cost-state` que cuadran, los tokens por
  modelo de `traza.report` coinciden **exactamente** con `modelUsage` (ejecutado en local contra
  datos reales; no se commitea ningún dato real).

## F2 — Caché SQLite + watcher (~1 día)

`traza/db.py` (esquema §6.2, WAL, `meta.cache_generation`), `traza/watcher.py`.

- Comando: `python -m traza.scan` construye `~/.traza/traza.db` desde todos los JSONL y muestra
  cuántas sesiones, agentes, peticiones, `unknown` e ignorados hay, y cuánto tardó.

**Hecho cuando:**
- Tests verdes: línea parcial, truncado (reproceso desde 0), fichero borrado (cascada),
  idempotencia (ejecutar `scan` dos veces da los mismos conteos), jerarquía de subagentes y
  huérfanos.
- `scan` sobre todo el disco real termina, y el tiempo del fichero de 43 MB queda anotado.
- Borrar `traza.db` y repetir `scan` da los mismos números.

## F3 — Servidor + lista de sesiones en vivo (~1 día)

**Antes de la UI:** `impeccable init` (PRODUCT.md) + `impeccable shape` para fijar la dirección
visual. `security-audit` en modo guía al tocar la frontera HTTP.

`traza/server.py` (FastAPI, solo `127.0.0.1`, validación `Host`/`Origin`, SSE con `id`, `gen` y
heartbeat), `static/index.html`, `static/app.js`, `static/styles.css`. Comando `traza serve`.

**Hecho cuando:**
- Al abrir el navegador se ven las sesiones con coste estimado; al lanzar una sesión nueva de
  Claude Code aparece en ≤ 1 s sin recargar.
- `curl` con `Host: evil.com` → rechazado (test).
- Borrar `traza.db` con el servidor abierto → el navegador detecta la nueva generación y recarga.

## F4 — Árbol de agentes, estados y coste por agente (~1 día)

**Hecho cuando:**
- Una sesión de prueba que lanza 2 subagentes (uno anidado) muestra el árbol correcto en vivo,
  con los estados de §7.1 cambiando mientras trabajan.
- Coste propio y acumulado por nodo coinciden con `traza.report` para esa sesión.
- Colapsar un nodo y recibir eventos SSE no lo vuelve a abrir.
- Columna "coste por token de salida" ordenable.

## F5 — Vista de juicio y contenido bajo demanda (~1 día)

`GET /content?ids=…` con verificación de `uuid`.

**Hecho cuando:**
- Al seleccionar un subagente se ven su encargo, su timeline por turnos (turnos cerrados
  plegados, últimos 50 + "cargar anteriores") y su resultado devuelto.
- Test: una salida de herramienta que contiene `<script>alert(1)</script>` se muestra como texto
  y no se ejecuta.
- Test: si el `uuid` en el offset no coincide, o el fichero ya no existe, se muestra
  "contenido no disponible".

## F6 — Señales y salud del parser (~1 día)

`traza/signals.py`: funciones puras con los umbrales en un bloque de constantes.

**Hecho cuando:**
- Un test por señal de §7.2 (incluidos: bucle **sin** error intermedio, reintento que funcionó
  **sin** alerta, subagente de larga duración **no** marcado como colgado).
- Provocar a propósito en una sesión real un error repetido → la alerta aparece en el nodo y lleva
  al evento.
- La barra global muestra `unknown`, tipos ignorados (con desglose) y coste interno no desglosado.

## F7 — Empaquetado y portfolio (~1 día)

- Instalación en un comando: `pipx install traza` (o `uv tool install`), a confirmar según
  disponibilidad del nombre (F0).
- README en inglés con: qué es y qué no, GIF de demo (revisado para que no muestre datos
  personales), contrato de caché y ventana rodante de 30 días, "coste estimado", resultado del
  test oráculo, cómo se verificó cada dato.
- Gates finales: `impeccable audit` + `impeccable critique`, `security-audit` completo,
  `/code-review`, verificación contra el panel en ejecución real.

**Hecho cuando:** en un entorno limpio se instala con un comando, `traza serve` abre el panel con
datos reales, y todos los gates pasan o sus hallazgos están resueltos o documentados.

---

## Estimación

~7 días de trabajo (F0–F7). Es una estimación, no un compromiso: F1 y F2 son las fases con más
riesgo de alargarse, porque dependen de un formato no documentado.

## Después de v1 (no planificado todavía)

- **v1.x:** hooks HTTP ("necesita input").
- **v2:** juez LLM local calibrado con casos etiquetados; salida de hooks en la timeline.
- **Ideas:** gráficas de gasto, alertas de presupuesto, exportar informe, replay.
