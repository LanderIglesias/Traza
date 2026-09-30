# traza — Hallazgos de F0

> Fecha: 2026-09-29 · Scripts de la investigación: desechables, no forman parte del repo.
> Datos: `~/.claude/projects/` de la máquina del autor (85.027 líneas JSONL), CLI 2.1.226,
> extensión de VS Code 2.1.267.

## 1. ¿Las líneas `assistant` se escriben en streaming o al terminar la respuesta?

**Respuesta: al terminar cada respuesta, todas las líneas de golpe.** [verificado en vivo]

- Prueba: sesión mínima con Haiku (`claude -p`), vigilando el tamaño del JSONL cada 0,1 s.
- Las 3 líneas de la petición `…AXjasM` (`thinking`, `text`, `tool_use`) aparecieron en el
  fichero **en la misma lectura** (t = 9,53 s), aunque sus `timestamp` están separados 0,6 s
  (21:02:02.101 → .301 → .709).
- En el historial, las líneas de una misma petición tienen `timestamp` distintos (6.990
  peticiones multilínea: mediana 1,5 s, p90 9,9 s, máx. 202 s, 0 % con diferencia 0). El
  `timestamp` es **cuándo se generó el bloque**, no cuándo se escribió la línea.
- Todas las líneas llevan ya el `usage` final y el `stop_reason`.

**Consecuencia:** el estado "pensando" solo se detecta por `mtime` (como decía el diseño). La
hora que se muestra de un evento es la del bloque, que es la útil para la timeline.

**Hallazgo extra:** la CLI 2.1.226 **no escribe `apiBlockIndex`**; las sesiones de VS Code
2.1.267 sí. El parser no puede depender de ese campo: el orden del bloque es el orden de la
línea en el fichero.

## 2. ¿`claude --resume` escribe en el mismo fichero o crea otro?

**Respuesta: en el mismo fichero** (CLI en modo `-p`). [verificado en vivo]

- Se reanudó la sesión de prueba `34b0105c…` con `--resume`: el fichero creció de 111.879 a
  126.109 bytes y no apareció ningún fichero nuevo.

**Consecuencia:** una sesión reanudada no genera copias. El par `7bc000bb` / `3416476c` lo
produjo otra cosa (ver §3).

## 3. Sesiones que comparten peticiones (`7bc000bb` / `3416476c`)

**Respuesta parcial.** [verificado lo que sigue; mecanismo no determinado]

- Comparten 1.274 `requestId` y **0 `uuid`** (la copia regenera los `uuid`).
- `usage` idéntico en las 3.407 líneas compartidas; 1.273 de 1.274 peticiones con el mismo
  `timestamp` en ambos ficheros.
- `7bc000bb` empezó a las 14:42; `3416476c`, a las 14:47 del 22-09.
- Las líneas compartidas están repartidas por **todo** el fichero copia (de la línea 25 a la
  21.954 de 21.974): no es una copia única al principio, sino dos ficheros que reciben las
  mismas peticiones a lo largo del tiempo.
- La copia no menciona el id de la original en ningún campo.
- Dentro de `3416476c` hay **4 `uuid` repetidos** (todos `attachment` de tipo `hook_success`,
  `PreToolUse:Bash`, líneas 1906–1919 y otra vez en 1957–1960, con el mismo `timestamp`).
  Parece que Claude Code **vuelve a escribir** esas salidas de hook unas 50 líneas después. En
  una de ellas solo cambia el `parentUuid`: se reenganchan a otro mensaje. **Pendiente de
  investigar por qué; no afecta a v1**: `attachment` se ignora y la idempotencia es por
  posición.
- El `cost-state` de `3416476c` (línea 13.299) tiene `startTime` del 23-09 07:37 y dice
  458.149 tokens de salida de `claude-opus-5-5`; en su ventana `[startTime, línea]` el JSONL
  tiene 864.273, **todas** peticiones compartidas con `7bc000bb`. No cuadra con ninguna ventana
  probada.

**No se pudo determinar** qué acción crea este par (candidatos: fork desde la extensión,
`bridge-session`) ni qué mide ese `cost-state`. Se agotó el tiempo asignado.

**Consecuencias (ya en `design.md` §6.2):** idempotencia por posición, no por `uuid`;
`requestId` global con sesión dueña = la que empezó antes; reasignación al borrar; estas
sesiones quedan fuera del test oráculo.

## 4. ¿Qué mide `cost-state`?

**Respuesta: el gasto del proceso de Claude Code desde que arrancó (`startTime`) hasta que se
escribe la línea**, en las sesiones sanas. [verificado en 2 sesiones]

- `36b96010` y `598796c2`: tokens por modelo **exactos** en la ventana
  `[startTime, posición de la línea]`.
- **En dólares también exacto**: con los precios oficiales (§5), `36b96010` da 2,012743 $
  (`claude-sonnet-5`) y 0,017852 $ (`claude-haiku-4-5`), idénticos a los `costUSD` de
  `cost-state`. Esto valida la tabla de precios y la regla de caché de 1 h.
- Las llamadas a `claude-haiku-4-5` del `cost-state` **no aparecen en el JSONL**: son
  internas de Claude Code.

**Consecuencia para el test oráculo:** compara en la ventana `[startTime, línea]`, no la
sesión entera.

## 5. Precios oficiales

Fuente: platform.claude.com/docs/en/about-claude/pricing, consultada el 29-09-2026. USD por
millón de tokens.

| Modelo | Input | Escritura caché 5 min | Escritura caché 1 h | Lectura de caché | Output |
|---|---|---|---|---|---|
| `claude-opus-5-5` | 4 | 5 | 8 | 0,20 (0,05×) | 20 |
| `claude-opus-5` | 5 | 6,25 | 10 | 0,50 | 25 |
| `claude-sonnet-5` | 2 | 2,50 | 4 | 0,20 | 10 |
| `claude-haiku-4-5` | 1 | 1,25 | 2 | 0,10 | 5 |

Modelos presentes en disco: `claude-sonnet-5` (12.205 líneas), `claude-opus-5-5` (6.304),
`claude-opus-5` (1.352), `claude-haiku-4-5-20251001` (100), `<synthetic>` (18).

**Modificadores de precio que existen y el diseño no contemplaba:**
- **Fast mode** (`usage.speed = "fast"`): Opus 5.5 a 8/40, Opus 5 a 10/50, con los
  multiplicadores de caché encima. El campo `speed` ya aparece en los JSONL.
- **`inference_geo = "us"`**: ×1,1 en todo. Los JSONL traen `usage.inference_geo`
  (visto `"not_available"`).
- **Búsqueda web**: 10 $ por 1.000 búsquedas (`usage.server_tool_use.web_search_requests`).

**Consecuencia:** `prices.toml` y el cálculo de coste deben leer `speed`, `inference_geo` y
`web_search_requests`. Un valor desconocido en cualquiera de ellos → coste `?`.

## Efectos secundarios de F0

- La prueba en vivo creó una sesión real en
  `~/.claude/projects/C--Users-Usuario-AppData-Local-Temp-traza-f0/` (2 respuestas de Haiku,
  coste de céntimos). **Borrada el 30-09-2026** junto con los scripts desechables de `%TEMP%`.

## F2 — Caché y watcher contra los datos reales (30-09-2026)

`python -m traza.scan` sobre todo `~/.claude/projects` (145 ficheros, 88.983 líneas):

- **Tiempos:** construcción completa 1,65–1,98 s; segunda pasada sin cambios 0,01 s (0 ficheros
  abiertos); el fichero mayor (`3416476c`, ahora 71 MB, 21.974 líneas) 0,56 s solo.
- **Reconstrucción:** borrar la BD y repetir da exactamente los mismos números; subir
  `PARSER_VERSION` la reconstruye sola (generación nueva).
- **Caché = parser puro:** 8.113 peticiones y tokens por modelo idénticos a los del parser leído
  a mano (excluida la sesión en curso, que crece mientras se compara).
- **Bug encontrado y corregido:** la sesión dueña salía **al revés** (la original `7bc000bb`
  "heredaba" de la copia). Causa: la copia empieza con el **mismo prompt y el mismo
  `timestamp`** (14:47:01.516) que la original; el watcher tomaba el inicio del primer evento y
  empataban, y el desempate por id favorecía a la copia. Solo la primera línea (ignorada)
  las distingue: `attachment` 14:42 en la original, `queue-operation` 14:47 en la copia. Ahora el
  parser da el `timestamp` de toda línea y el inicio es el de la primera. Test con el caso real
  (incluido el id de la copia ordenando antes). Resultado: `3416476c` hereda **1.274**
  peticiones de `7bc000bb`, la cifra de F0.
- **Subagentes huérfanos:** 6 de 88. Son los únicos `meta.json` **sin `toolUseId`** (ni
  `description`); uno es el `/code-review` de esta sesión, lanzado por una skill en modo fork.
  Los 82 con `toolUseId` encuentran a su padre. **Patrón confirmado** (revisión de F2): ahora
  son 7 y los 7 son forks de la skill `/code-review` (su encargo empieza por "Review target:" o
  "medium effort → … angles"), de 4 sesiones distintas. No es un fallo al leer `meta.json`:
  Claude Code no escribe `toolUseId` cuando una skill lanza un subagente en modo fork. Enlazarlos
  (p. ej. por hora con el `tool_use` de la skill) queda **sin investigar**.
- **Sin verificar con datos reales:** subagentes anidados (0 en disco con padre distinto de
  `main`; la regla solo está probada con fixtures) y el truncado/borrado de un fichero vivo (solo
  en tests sobre copias).
- **Revisión de código de F2** (`/code-review`): tres fallos corregidos con test, ninguno visto
  en los datos reales pero todos posibles: (1) una línea con valores de tipo raro (dict donde va
  texto, entero > 2^63) haría fallar el `INSERT`, desharía el tick y lo repetiría para siempre;
  (2) `connect()` borraba cualquier fichero de otra cosa pasado como `--db`; (3) un fichero
  sustituido por otro igual o más largo no se detectaba. Tras los arreglos, el disco real da los
  mismos números (8.113 peticiones = parser puro, copia hereda 1.274); la construcción completa
  midió 4,1 s en esa pasada (1,7–2 s en las anteriores; sin aislar si es ruido o la lectura extra
  de la primera línea).
- **Tiempo de construcción:** tres construcciones completas seguidas, 1,71 / 1,72 / 1,74 s. La
  medida de 4,1 s no se repite. Perfil: 0,73 s decodificando JSON, 0,27 s en `executemany`, el
  `glob` no aparece. No hay coste escondido.
- **Segunda revisión de código de F2** (`/code-review` sobre `d5d87c4`): 7 fallos, corregidos con
  test que se vio fallar antes: dos escáneres a la vez leían offsets sin cerrojo; otro escritor
  fallaba a los 5 s; raíz relativa = todo "borrado" y releído; `meta.json` tardío o a medias
  marcaba al subagente como huérfano "para siempre"; un Ctrl+C en la primera creación dejaba una
  BD que `connect` rechazaba; truncar el principal conservaba el título viejo; cambio de versión
  con la BD abierta reventaba en Windows. Tras los arreglos, el disco real: 8.115 peticiones =
  parser puro, copia hereda 1.274, 8 huérfanos todos `sin_tool_use_id`, 0 `meta.json` sin leer.
- **Tests que pasaron en verde a la primera** (describían comportamiento ya existente): a los 5 se
  les rompió el código a propósito (mutación) y los 5 fallaron. Rutina desde ahora: un test nuevo
  no se da por bueno sin haberlo visto fallar.

## F3 — Servidor y pantalla de sesiones contra los datos reales (30-09-2026)

- **`stop_reason` no sirve para saber si un turno terminó.** Las versiones 2.1.24–2.1.27 lo
  escriben `null` en muchas líneas, también en la respuesta final: 42 ficheros acaban en
  `(stop_reason = null, text)`. Corrige la generalización de §1 (medida con la CLI 2.1.226). El
  autómata usa "la última línea es texto del modelo" en su lugar.
- **Qué prompts contesta el modelo** (siguiente línea `user`/`assistant` es del modelo): humano
  387/390 (99 %), `isMeta` 130/142 (92 %), `task-notification` 84/91 (92 %), sin origen
  100/131 (76 %), interrupciones 1/9, salidas de comando local 0/21.
- **Herramientas "en vuelo" viejas:** una regla literal ("`tool_use` sin su `tool_result`")
  dejaba sesiones en `tool` para siempre por herramientas interrumpidas; solo cuentan las de la
  última respuesta del agente (hallado con la fixture de la copia).
- **Los errores de herramienta son sobre todo bloqueos de hooks:** de 892 `tool_result` con
  `is_error`, 515 (58 %) son hooks que bloquean (GateGuard y similares), 157 `Exit code`, 4
  rechazos del usuario, 216 otros. El badge de F3 los cuenta todos y lo dice en su tooltip;
  separados en F4 (ver §F4).
- **Título "(fork)":** `7bc000bb`, a la que la regla de dueña (empezó antes) asigna las 1.274
  peticiones compartidas, lleva un `custom-title` "…(fork)" que aparece en su línea 10.317, no
  al principio. **Sin determinar** si Claude Code etiqueta así la sesión nueva o la de origen;
  si fuera la nueva, la regla de dueña asignaría el coste a la copia. No afecta al total (cada
  petición cuenta una vez), solo a en qué fila aparece.
- **Latencia de una sesión nueva:** 0,07–0,28 s desde que aparece el fichero hasta el evento SSE
  y la API (5 medidas, con un servidor sobre carpeta temporal para no ensuciar el historial).
- Pantalla verificada en Chrome a 1440×900 y 390×844 (emulación móvil): sin desbordamiento
  horizontal (`scrollWidth` = 390), consola sin errores, detector de impeccable sin hallazgos.
- **Revisión de código del commit F3** (`8cf0d49`, más `1b58922`): 6 fallos, corregidos con test
  visto fallar: un `timestamp` sin zona tumbaba `/api/overview` (500); la medianoche local se
  desplazaba una hora en los días de cambio de hora; al apagar con una pestaña abierta uvicorn
  esperaba 3 s y registraba un ERROR (los streams SSE se cierran ahora al recibir la señal, antes
  de que uvicorn espere a las conexiones); el navegador se abría aunque el servidor no llegara a
  arrancar; respuestas desordenadas de la misma sesión podían pintar un estado viejo; una
  etiqueta `<command-name>` en medio de un prompt humano lo reclasificaba como comando local
  (1 caso en disco). Indicador de conexión probado en Chrome: verde → ámbar (~3 s) → rojo
  (+10 s) → verde (~2,5 s tras volver el servidor, sin recargar).

## F4 — Árbol de agentes (30-09-2026)

- **Bloqueo vs error real: `toolDenialKind`.** Sobre una copia congelada (922 `tool_result` con
  `is_error`): el campo de línea `toolDenialKind` aparece **solo** en resultados con error y dice
  que la herramienta no se ejecutó: `permission-rule` 669 (462 hooks con prefijo
  `PreToolUse:<Tool> hook error:`, 205 hooks **sin** prefijo, 2 reglas internas),
  `automode-blocked` 6, `automode-unavailable` 7, `user-rejected` 2. Sin él (238): `Exit code`,
  timeouts, `<tool_use_error>` — fallos reales. El prefijo de hook solo existe desde 2.1.278
  (antes el texto del hook va tal cual), así que clasificar por texto habría exigido reconocer
  el mensaje de cada plugin; `toolDenialKind` (visto desde 2.1.259) no. Anteriores a 2.1.259: sin
  campo; en disco no hay bloqueos de hook en esas versiones.
- **Primera reconstrucción real por cambio de esquema** (parser 5→6, esquema 3→4, columna
  `events.denial`): misma copia congelada, antes y después, **idénticos** en ficheros, sesiones,
  agentes, peticiones, refs, eventos por tipo, tokens, coste ($981,05), dueñas, errores por
  sesión e ignorados. Build completo 1,91 s → 1,90 s. Navegador con el servidor viejo abierto →
  servidor nuevo sobre la misma caché: la reconstruye (generación nueva) y la pestaña recarga
  sola (`navigation.type = "reload"`).
- **Bug de F1 corregido: `output_tokens` se tomaba de la primera línea.** En 374 peticiones el
  mismo `requestId` aparece en varias líneas con `output_tokens` que **crece** (8 → 257); input y
  caché no cambian. "Gana la primera" contaba 1.827 tokens de salida en vez de 207.240. Ahora:
  `output` = el mayor visto (parser y upsert del watcher, entre ticks). Sobre la copia congelada
  solo cambia eso: +205.413 tokens de salida, coste total $981,05 → **$983,43**. Los tests no lo
  veían porque la fixture no tenía el caso y el oráculo comparaba dos caminos con la misma regla.
  Nota: en 2.1.284 hay respuestas cuyo `output_tokens` se queda en 1 en **todas** sus líneas
  (p. ej. texto + tool_use); ese número está mal en el propio JSONL y traza no puede corregirlo.
- **Carrera en lectura (500 en vivo).** Cada endpoint hace varias consultas; sin transacción, un
  commit del watcher entre dos de ellas daba `KeyError` en `agent_tree` (petición vista por una
  consulta y no por la otra) y el panel se vaciaba. Ahora cada petición HTTP lee en **una**
  transacción de lectura (foto coherente en WAL). Además el cliente solo suelta la sesión
  seleccionada ante un 404, no ante cualquier error.
- **"Terminado" de un subagente — cómo lo escribe Claude Code** (93 subagentes en disco):
  `toolUseResult.status` en el `tool_result` del padre: `async_launched` 67 (segundo plano: es el
  **lanzamiento**, no el fin), `completed` 15, `forked` 11 (skill fork, con su `agentId`: permitiría
  enlazarlo con su padre; no implementado, ver pendientes). En segundo plano el fin llega como
  `<task-notification>` con `<task-id>` y `<status>` (`completed`/`failed`/`stopped`): como prompt
  si el agente estaba parado, o **encolada** como `attachment` `queued_command` con
  `commandMode: "task-notification"` si estaba ocupado (visto solo en vivo, 2.1.284). Dentro de un
  subagente, el resultado de un hijo de primer plano no lleva `toolUseResult`. Con las tres
  fuentes: 90 de 93 "done"; los 3 restantes nunca tienen fin registrado (2 están en un resumen
  "No completion record was found… may have been stopped" que nombra varias tareas y no se
  atribuye).
- **Estado en vivo de una respuesta a medias.** 2.1.284 escribe las líneas de una respuesta de
  subagente mientras se genera: la línea de texto que precede a una herramienta lleva
  `stop_reason` null (6/6) y la herramienta llega 0,4–1,3 s después; en el hilo principal esa
  línea ya lleva `tool_use` (159/159). La línea final de texto lleva `end_turn` (47/47); null
  final solo en versiones ≤ 2.1.268 (44). Sin tratarlo, el panel parpadeaba "idle" a mitad de
  trabajo (visto en la prueba en vivo). Regla en design.md §7.1; `stop_reason` de una petición =
  el último no nulo.
- **Prueba en vivo de F4** (esta sesión, 3 tandas de 2 subagentes, uno con un hijo anidado):
  árbol correcto (nieto bajo su padre), **ningún salto de huérfano** (contador de inserciones por
  `agent_id` con MutationObserver: cada agente en una sola posición en 24–32 re-renders), plegar
  un nodo aguanta 27 ticks SSE sin reabrirse, estados thinking ↔ tool sin "idle" intermedio tras
  la corrección, y los 10 subagentes acaban en "done".
- **Coste por nodo = `traza.report`:** copia congelada, 57 sesiones, 150 nodos, 0 diferencias;
  acumulado de main + huérfanos = total de report en cada sesión (se excluye la copia del par
  "(fork)": report no conoce dueñas).
- **`/code-review` de F4 (8dbdaab..56a9d3c), 4 hallazgos, todos corregidos con test:** un
  resultado sin estado contaba como fin aunque llegara antes del trabajo del hijo; "done" no se
  quitaba si el agente se reanudaba (ambos: la prueba de fin debe ser posterior a la última línea
  del agente; sobre la copia congelada siguen 90 de 93 "done"); la notificación en bloques de
  texto se perdía; el foco del botón de plegar se perdía en cada re-render (verificado en Chrome
  tras 2 re-renders). De paso: `session_summary` calculaba los estados dos veces (667 → 350 ms).
