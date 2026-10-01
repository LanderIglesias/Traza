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

## Ya hecho (29-09-2026, durante el brainstorming)

- Deduplicación por `requestId` y test oráculo **validados con un script desechable** contra
  los JSONL reales: tokens exactos en 2 de 3 sesiones con `cost-state`.
- Nombre `traza` **libre**: `https://pypi.org/pypi/traza/json` → 404; en GitHub solo hay
  proyectos pequeños de otros ámbitos (modelado 3D, transporte, blockchain).
- Repo git creado con el commit inicial de `docs/`.

## F0 — Verificaciones previas (spike, ~medio día) — ✅ HECHO, ver [`findings.md`](findings.md)

Resolver los **[sin verificar]** del diseño con pruebas reales. Todo el código de esta fase es
**desechable** y no entra en el repo.

| Pregunta | Cómo se comprueba |
|---|---|
| ¿Las líneas `assistant` se escriben al terminar la respuesta o en streaming? | comparar marcas de tiempo de las líneas de un mismo `requestId` en una sesión grabada a propósito |
| ¿`claude --resume` escribe en el mismo fichero o crea otro? | reanudar una sesión de prueba y observar `~/.claude/projects/` |
| Sesiones copiadas (`3416476c` copia de `7bc000bb`): ¿qué acción las crea (fork, `--resume`)? ¿Las líneas copiadas conservan sus `timestamp` originales? ¿Qué regla decide la sesión dueña de una petición si la copia se ingiere antes? ¿Qué representa su `cost-state`? | comparar ambos ficheros línea a línea (`requestId`, `timestamp`, `parentUuid`) y reproducirlo con una sesión de prueba |
| Precios actuales de los modelos presentes en disco | referencia oficial de precios de Anthropic |

**Hecho cuando:** `docs/findings.md` responde cada pregunta con la evidencia (o "no se pudo
determinar" y su consecuencia), y `design.md` está actualizado si algo cambió.

## F1 — Parser puro + cálculo de tokens (~1 día) — ✅ tests verdes, pendiente de revisión

**Orden obligatorio:** los dos primeros tests que se escriben son la **deduplicación por
`requestId`** y el **parser sobre una fixture con la forma real de un JSONL**. Si fallan, todo lo
demás miente.

Configurar el repo (`.gitignore` que excluya `.claude/`, `.serena/`, `*.db` y cualquier
JSONL real), `pyproject.toml`, `traza/parser.py`, `traza/pricing.py`, `prices.toml`, tests.

- Fixtures **sintéticas y sanitizadas** que reproducen los casos reales: petición multilínea con
  `usage` repetido, `<synthetic>`, `assistant` sin `requestId`, `tool_use`/`tool_result`,
  `api_error`, `compact_boundary`, tipo desconocido, subagente con `meta.json`.
- Comando de verificación: `python -m traza.report <fichero.jsonl>` imprime agentes, peticiones y
  tokens por modelo.

**Hecho cuando:**
- Tests verdes: deduplicación por `requestId`, cada tipo de línea, `NULL` vs 0, `<synthetic>` = 0,
  modelo desconocido = `?`, alias solo de sufijo de fecha.
- Test de peticiones heredadas: dos fixtures que comparten `requestId` → el coste total cuenta
  cada petición una vez.
- **Test oráculo** (política de `design.md` §8): recorre todas las sesiones con `cost-state`,
  informa cuáles cuadran; exige coincidencia **exacta** de tokens por modelo en la lista de
  sesiones sanas (`36b96010`, `598796c2`) **que sigan en disco** (las ausentes se
  informan, no fallan); las copias se informan como *known issue*. Corre solo
  en local contra datos reales; no se commitea ningún dato real.

## F2 — Caché SQLite + watcher (~1 día) — ✅ tests verdes, pendiente de revisión

`traza/db.py` (esquema §6.2, WAL, `meta.cache_generation`), `traza/watcher.py`.

- Comando: `python -m traza.scan` construye `~/.traza/traza.db` desde todos los JSONL y muestra
  cuántas sesiones, agentes, peticiones, `unknown` e ignorados hay, y cuánto tardó.

**Hecho cuando:**
- Tests verdes: línea parcial, truncado (reproceso desde 0), fichero borrado (cascada),
  idempotencia (ejecutar `scan` dos veces da los mismos conteos), jerarquía de subagentes y
  huérfanos.
- `scan` sobre todo el disco real termina, y el tiempo del fichero de 43 MB queda anotado.
- Borrar `traza.db` y repetir `scan` da los mismos números.

## F3 — Servidor + lista de sesiones en vivo (~1 día) — ✅ tests verdes, pendiente de revisión

**Antes de la UI:** `impeccable init` (PRODUCT.md) + `impeccable shape` para fijar la dirección
visual. `security-audit` en modo guía al tocar la frontera HTTP. (`impeccable` y
`security-audit` son herramientas del flujo de trabajo del autor, **no dependencias** del
proyecto.)

`traza/server.py` (FastAPI, solo `127.0.0.1`, validación `Host`/`Origin`, SSE con `id`, `gen` y
heartbeat), `static/index.html`, `static/app.js`, `static/styles.css`. Comando `traza serve`.

**Hecho cuando:**
- Al abrir el navegador se ven las sesiones con coste estimado; al lanzar una sesión nueva de
  Claude Code aparece en ≤ 1 s sin recargar.
- `curl` con `Host: evil.com` → rechazado (test).
- Un navegador conectado con una generación de caché distinta (la BD se reconstruyó: cambio de
  versión o reinicio tras borrarla) recibe `reload` por SSE y recarga (test). Nota: en Windows no
  se puede borrar `traza.db` con el servidor abierto (el watcher la tiene abierta).

## F4 — Árbol de agentes, estados y coste por agente (~1 día)  ✅ tests verdes y prueba en vivo, pendiente de revisión

**Hecho cuando:**
- Una sesión de prueba que lanza 2 subagentes (uno anidado) muestra el árbol correcto en vivo,
  con los estados de §7.1 cambiando mientras trabajan.
- Coste propio y acumulado por nodo coinciden con `traza.report` para esa sesión.
- Colapsar un nodo y recibir eventos SSE no lo vuelve a abrir.
- El subagente **no** aparece un instante como huérfano antes de colocarse bajo su padre
  (design.md §6.1, decisión "huérfano temporal"). Si aparece, la UI retrasa un tick los
  `padre_no_encontrado` recientes.
- Huérfanos con marca distinta según `db.orphans`: `sin_tool_use_id` se muestra como "fork de
  skill" con el principio de su encargo; `padre_no_encontrado` como "padre desconocido".
- Columna "coste por token de salida" ordenable. (En móvil las filas son tarjetas sin cabecera: sin orden por columna.)

## F5 — Vista de juicio y contenido bajo demanda (~1 día)  ✅ tests verdes y verificado en Chrome, pendiente de revisión

`GET /content?ids=…` con verificación de `uuid`.

**Hecho cuando:**
- Al seleccionar un subagente se ven su encargo, su timeline por turnos (turnos cerrados
  plegados, últimos 50 + "cargar anteriores") y su resultado devuelto.
- Test: una salida de herramienta que contiene `<script>alert(1)</script>` se muestra como texto
  y no se ejecuta.
- Test: si el `uuid` en el offset no coincide, o el fichero ya no existe, se muestra
  "contenido no disponible".

## F6 — Señales y salud del parser (~1 día)  ✅ tests verdes y verificado en vivo, pendiente de revisión

`traza/signals.py`: funciones puras con los umbrales en un bloque de constantes.

**Hecho cuando:**
- Un test por señal de §7.2 (incluidos: bucle **sin** error intermedio, reintento que funcionó
  **sin** alerta, subagente de larga duración **no** marcado como colgado).
- Provocar a propósito en una sesión real un error repetido → la alerta aparece en el nodo y lleva
  al evento.
- La barra global muestra `unknown`, tipos ignorados (con desglose) y coste interno no desglosado.

## F7 — Empaquetado y portfolio (~1 día)

**Antes de empezar (revisión de F6):** usar el panel **un día entero de trabajo real sin escribir
código** — prueba de aceptación personal: ¿aparecen las señales cuando toca?, ¿cuadra el árbol
con lo que recuerdas?, ¿es creíble el coste por subagente?, ¿hay lentitud, tooltips que tapan,
scroll que salta? Lo que salga se arregla antes del README y del GIF (el GIF no se rehace).
Posible historia para el README: el 92 % de los reintentos que funcionan **cambian la llamada**
(los agentes corrigen sus errores), findings.md "Antes de F7".

- Instalación en un comando **desde el repo**: `pipx install git+https://github.com/<usuario>/traza`.
  Publicar en PyPI (`pipx install traza`) solo si el tiempo lo permite.
- README en inglés con: "el primer arranque construye la caché (~2 s con 90k líneas, más con
  disco frío o antivirus); los siguientes solo leen lo nuevo", qué es y qué no, GIF de demo (revisado para que no muestre datos
  personales), contrato de caché y ventana rodante de 30 días, "coste estimado", resultado del
  test oráculo, cómo se verificó cada dato.
- **Cifras del README y de las capturas con los números corregidos en F4** (el coste total
  subió de $981,05 a $983,43 al corregir `output_tokens`): regenerarlas al final, no copiar
  cifras de fases anteriores.
- README, límites de lo que Claude Code escribe (no son bugs de traza): subagentes sin fin
  registrado en disco (3 de 93) se quedan en "idle" con tooltip; `output_tokens` mal escrito
  (aviso de plausibilidad); skill forks sin enlazar a su padre (decisión, design.md §6.1).
- **Notas de la revisión de F5** (decidir y probar antes de cerrar F7):
  1. "Load earlier" no tiene tope (300 turnos = 6 clics), no se puede volver a plegar y se
     resetea al cambiar de agente: decidir si basta o hace falta "cargar todos" / "volver a 50".
  2. Scroll con turnos anteriores cargados y un tick nuevo: comprobar que la vista no salta.
  3. Arranque tras reconstruir la caché (panel sin sesiones mientras dura el primer escaneo):
     **medir** con dos tamaños de disco y anotar si crece; decisión consciente, no "~2 s".
  4. Recorte a 20.000 caracteres con una salida real así de larga dentro de una vista con 50
     turnos desplegados: medir el tiempo de render.
  5. Mensaje "terminó sin escribir texto": verlo en pantalla con un subagente forzado a
     devolver sin texto.
- **Decidir antes de cerrar F7:** truncado/sustitución de un fichero **en vivo** (probado solo
  con tests): o se prueba con un fichero real mientras corre el servidor, o se documenta como
  limitación.
- Gates finales: `impeccable audit` + `impeccable critique`, `security-audit` completo,
  `/code-review`, verificación contra el panel en ejecución real.

**Hecho cuando:** en un entorno limpio se instala con un comando, `traza serve` abre el panel con
datos reales, y todos los gates pasan o sus hallazgos están resueltos o documentados.

---

## Estimación

~7 días de trabajo (F0–F7). Es una estimación, no un compromiso: F1 y F2 son las fases con más
riesgo de alargarse, porque dependen de un formato no documentado.

**Si hay que recortar, se cortan F6 y F7**, en ese orden inverso: primero el empaquetado
pulido (F7: se puede usar con `python -m traza serve` sin instalador), después las señales
automáticas (F6). F1–F5 ya dan el panel que se usa a diario: sesiones, árbol, coste y vista de
juicio.

## Después de v1 (no planificado todavía)

- **v1.x:** hooks HTTP ("necesita input").
- **v2:** juez LLM local calibrado con casos etiquetados; salida de hooks en la timeline.
- **Ideas:** gráficas de gasto, alertas de presupuesto, exportar informe, replay.
