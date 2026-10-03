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

## F5 — Vista de juicio (01-10-2026)

- **Datos reales:** la vista del agente principal de esta sesión (635 elementos) devuelve los
  últimos 50 turnos en 0,40 s y 21 KB; "Load earlier" pasa a 100 turnos y un tick posterior
  añade el turno nuevo sin perder los cargados (101). Un skill fork de `/code-review` muestra
  su encargo completo, 7 turnos y su respuesta final. Móvil (390 px): sin desbordamiento.
  En vivo, el turno en curso aparece con "No result yet" en sus herramientas.
- **Plausibilidad de `output_tokens` (aviso, §8):** 385 de 8.811 peticiones en disco declaran
  menos de 1 token por cada 40 caracteres escritos; se cuentan en la barra de salud y se
  marcan con `?` en su agente y en su turno.
- **Primer arranque tras subir versión:** durante el escaneo inicial (~2 s) `/api/overview`
  responde con 0 sesiones y una vista de agente da 404. Es el comportamiento desde F3 (el
  servidor no espera al primer escaneo); el siguiente tick lo corrige. Sin cambio.
- **`/code-review` de a498c8a..fc7c1be, 4 hallazgos, todos corregidos:** el turno abierto por
  defecto quedaba "elegido por el usuario" para siempre (`toggle` salta también al abrirlo
  desde código: ahora la preferencia se fija en el clic sobre `<summary>`; verificado en Chrome:
  al llegar un turno nuevo, el anterior se cierra); al cambiar de agente se veía durante
  milisegundos el encargo del anterior (ahora la vista se vacía al abrir; verificado: secuencia
  vacío → B); un agente `done` sin texto decía "still working"; un agente reanudado tras
  entregar mostraba lo de la primera vez (el resultado del padre solo vale si es posterior a su
  última línea, como "done"; test). Rendimiento revisado y sin cambio: `implausible_output`
  tarda 20 ms con la caché real (overview 196 ms, session_summary 306 ms, vista de main 228 ms).

## Antes de F6 (01-10-2026)

- **Los 385 avisos de plausibilidad, verificados a mano** (10 al azar, semilla 7): las 10 son
  reales. Ejemplos: 7 tokens declarados para una revisión de seguridad de 10.254 caracteres;
  3 tokens para un comando Bash de 315 caracteres. Rango 66–1.465 caracteres/token (umbral:
  40). Todas con `stop_reason` null en **todas** sus líneas y de 2.1.247–2.1.263: Claude Code
  no escribió la línea final de esas respuestas, la que lleva el `usage` completo.
- **Política de `?` al agregar:** con "algún sumando sospechoso", 70 de 163 agentes llevaban
  `?` (43 %: la marca no decía nada); con "la suma es implausible", 24.

## F6 — Señales (01-10-2026)

- **Calibración contra disco** (116 agentes, 11.544 llamadas a herramientas): el bucle de la
  especificación ("≥ 3 veces, sin error intermedio") daba **90** alertas, casi todas
  legítimas (38 `Read` releyendo, 26 `Bash` repitiendo `pytest`, 8 capturas); exigir 3 llamadas
  idénticas **seguidas** da **2**, ambas bucles reales (un clic repetido en el mismo botón, la
  misma lectura de un fichero de salida 3 veces). Reintento fallido (misma herramienta,
  cualquier input): 34, y 8 revisados al azar son agentes atascados de verdad
  (`request_access` denegado una y otra vez, `python -c` fallando dos veces, Glob con timeout
  dos veces). Con el mismo input serían solo 3: se descartó por perder casos reales.
- **Prueba en una sesión real (esta):** dos `cat` de ficheros inexistentes seguidos →
  "failed retry" en el turno del segundo; el badge del nodo `main` lleva a la alerta más
  reciente (que resultó ser otro fallo real mío: dos `evaluate_script` que fallaron seguidos),
  con el turno abierto, centrado y con el foco; el resaltado sobrevive a los ticks.
- **Bug encontrado al probar: caché del navegador.** Sin `Cache-Control`, Chrome sirvió un
  `index.html` viejo con el `app.js` nuevo: `renderCards` fallaba en un elemento inexistente y
  el panel se quedaba vacío y "Disconnected". Ahora `no-cache` (revalidación por ETag);
  verificado recargando **sin** forzar: llegó el JS nuevo.
- **`/code-review` de F6, 2 hallazgos (falsas alarmas en rojo), corregidos con test y
  mutación:** llamadas en paralelo de la misma respuesta contaban como reintento (sobre la misma
  caché: reintentos fallidos 37 → 35, reintentos que funcionaron 202 → 197); un agente `done`
  podía mostrar una herramienta "colgada" para siempre (0 casos hoy en disco; la regla de §7.1
  ya decía que "terminado" manda y las señales no lo aplicaban).

## Antes de F7 (01-10-2026)

- **Duración real de las herramientas** (11.564 llamadas con resultado): mediana 2 s, p99
  160 s, p99,9 603 s. Las 21 de más de 10 min: 12 `Bash` en su tope de 10 min (timeouts
  reales) y 9 `AskUserQuestion`/`ExitPlanMode` de hasta **115 min** esperando una respuesta
  humana. Sin excluirlas, "colgada" las habría marcado en rojo. De aquí salen las reglas de
  "colgada" (sesión que escribe en los últimos 30 min) frente a "sin terminar" (gris).
- **Coste interno medido:** $0,019 en 2 sesiones con `cost-state` (0,88 % y 0,66 % de su
  gasto); 0,03 % de los $55 que cubren todos los `cost-state` del disco. Es pequeño: la nota fija
  anterior ("internal cost not broken down") lo hacía parecer un agujero de tamaño desconocido.
- **Cifra de referencia del `?` de plausibilidad:** 24 de 163 agentes (15 %) llevan `?` el
  01-10-2026, 390 peticiones de 8.8k. Si Claude Code corrige el `usage`, debe bajar; si sube,
  hay más versiones o flujos afectados.
- **Los 198 "reintentos que funcionaron"** se concentran en 32 de 116 agentes; 150 son `Bash`.
  183 (92 %) **cambiaron la llamada** (otra ruta, otro comando, otra forma de citar): el agente
  corrige su propio error. Solo 15 repitieron exactamente lo mismo (fallo pasajero). No es "el
  modelo repite porque no recuerda".
- **Build completo:** 2,53–2,86 s con 101.335 líneas (un 7,3 s aislado en la primera medida,
  disco frío o carga de fondo; no se repitió).
- **`no-cache` cuesta ~2 ms por fichero** (304 con ETag); los estáticos suman 223 KB, casi todo
  la fuente. Imperceptible.
- **Móvil (390 px), dos fallos que no se veían en escritorio, corregidos:** el título de la
  sesión en la cabecera del panel se partía una palabra por línea (el estado y el coste no se
  plegaban), y en las filas de sesión con badges el título quedaba en una letra o desaparecía
  (este venía de F4, al añadir el chip de bloqueos). Ahora la cabecera y la fila hacen salto de
  línea; verificado también que en escritorio no cambia nada.
- **`/code-review` de 4b2ab68: sin fallos en "colgada"/"sin terminar"; 3 hallazgos del coste
  interno, corregidos:** un `cost-state` con el campo renombrado se habría leído como medida
  vacía y la cifra habría desaparecido sin aviso (ahora es `unknown`, test); la cifra es un
  mínimo y la interfaz ya lo dice ("at least"); design.md §6 aún decía que `cost-state` se
  ignora. Comprobación independiente del revisor sobre la caché real: $0,0188 en 2 sesiones,
  0,034 % de $54,97, la sesión copia no cuenta.
- **Estado "pensando" del principal — aclaración (pregunta del revisor):** la regla no es
  "última línea de texto → idle" sino "última línea de texto **del modelo** → idle". Tras un
  prompt tuyo el principal está `thinking` (test `("prompt", False, "thinking")`), también tras
  cada `tool_result`; pasa a `idle` con su texto final, con un prompt que no se contesta
  (interrupción, comando local) o con 10 min sin escribir. Visto en vivo en esta sesión: la
  tarjeta "Live now" decía "Thinking" mientras generaba. No hace falta un estado "enviado".

## Día de uso (01-10-2026)

- **"Estimated cost" se leía como dinero gastado.** El autor usa Claude Code con suscripción y
  no entendía de dónde salían 200+ $ por sesión. La cifra es el valor a precios de la API (lo
  que costaría pagando por token), no un gasto. Cambiado a "Value at API prices" / "Own value"
  en tarjetas, columnas, títulos y barra de salud; el cálculo no cambia. Pendiente de decidir:
  comparar con la cuota mensual del usuario (necesita que la configure).
- **Tipografía "cortada y pixelada" (día de uso).** Manrope es variable (peso 200–800) pero sin
  hinting; a 12–13 px en Windows se come los espacios entre palabras y desiguala los trazos
  (comparativa a 1× ampliada: "Output tok." → "Outputtok."). Segoe UI Variable, con hinting, se
  lee limpia a los mismos tamaños. Decisión del autor (de tres opciones): **mixta** — Manrope
  para display (≥ 16 px), fuente del sistema para texto pequeño y datos. Escala reducida a 7
  tamaños con papel y el 800 solo en display. Detector de impeccable limpio; verificado en
  escritorio (1×) y móvil (390 px).

## F6.5 — medidas para la vista de trazas (02-10-2026)

- **Sesiones (59, agente principal):** duración de reloj mediana ~0 h, p90 88,5 h, máx. 264 h;
  en las sesiones de más de 2 h de reloj, el activo es el 0–36 % (casi siempre < 10 %): una
  timeline a escala de reloj sería una raya. (Las cifras de tiempo activo y desglose de esta
  sección se recalcularon el 02-10-2026 tras los bugs de "Antes de F7 — Timeline legible"; ver
  el punto siguiente.)
- **Subagentes (106):** duración mediana 1,9 min, p90 5,0 min, máx. 64 min.
- **Desglose del tiempo activo del principal** (`timeline.breakdown` corregido, 02-10-2026, 65
  sesiones): tiempo de trabajo p90 1,4 h, máx. 15,5 h. Las cinco mayores — modelo /
  herramientas / tú (inactivo aparte): 7bc000bb 15,5 h — 33/58/10 (220 h); e093c05a 13,6 h —
  55/37/8 (251 h); 3416476c 12,2 h — 36/54/10 (162 h); 0d541f67 6,0 h — 47/37/16 (84 h);
  0d6a5565 5,3 h — 58/27/15 (65 h; sesión aún en curso). Invariante comprobado en los 193
  agentes del disco: ningún desglose supera el tiempo activo de su sesión.

## F6.5 — Vista de trazas (02-10-2026)

- **Timeline de la sesión real 0d6a5565:** 20 filas (principal + subagentes, orden del árbol),
  910 barras de turno; 4 h 26 m activos, 58 h 52 m de pausas eliminadas del eje (20 pausas,
  marcadas con línea sin ancho); calculada en 13 ms, 125 KB → con posiciones a décimas, menos.
  La sesión de 277 $: 1.458 barras, 100 pausas, 18 ms.
- **Barra de valor de un subagente (Haiku):** 73 % escrituras de caché, 22 % lecturas de caché,
  5 % salida. (El desglose de tiempo que se anotó aquí se calculó con la regla con el bug de
  "Antes de F7" y se ha borrado; las cifras buenas están en "F6.5 — medidas".)
- **En vivo:** dos subagentes `Explore` lanzados a la vez aparecen como filas nuevas con barras
  **solapadas** en el eje (16.082,5 s y 16.082,6 s de inicio); pulsar una barra abre la vista de
  juicio en ese turno, desplegado y resaltado. Duraron ~9 s en una sesión de 4 h 27 m activas:
  rayas de 3 px (límite de escala; zoom a v2).
- **Tema oscuro, contraste medido** (WCAG): tinta/tarjeta 15,3; tinta secundaria/tarjeta 7,5;
  secundaria/bloque hundido 6,9; terciaria/tarjeta 4,73; estados (herramienta, pensando,
  inactivo, error, terminado) 6,7–8,3. Fallos vistos y corregidos: la fila de sesión
  seleccionada tenía un fondo claro escrito a mano (ilegible en oscuro) → token `--selected`; el
  botón "All" pulsado no se distinguía en oscuro → tinta invertida.
- **Refactor sin regresión:** `request_cost` es ahora la suma de `request_cost_parts` (la barra
  de valor sale de la misma fórmula); el oráculo (tolerancia 1 µ$) sigue verde.
- **Mutaciones del desglose:** 7 de 8 fallaron sus tests; la que sobrevivió era código muerto
  (una comprobación de "misma respuesta" tras un texto que nunca cambiaba el resultado), quitado.
- **`/code-review` de F6.5, 5 hallazgos, todos corregidos:** (1) esperar a una herramienta
  larga contaba como pausa (≥ 5 min) en el desglose y en el eje — ahora una herramienta en
  marcha es trabajo dure lo que dure, y el eje común no corta los huecos que cubre (tests; el
  tiempo activo de 0d6a5565 pasa de 4 h 26 m a 4 h 34 m); (2) con herramientas en paralelo el
  tramo se cortaba en el primer resultado — ahora llega al último y queda abierto si alguna
  sigue en marcha (test); (3) la Timeline robaba el foco en cada tick — ahora no se redibuja si
  nada cambió y devuelve el foco a la misma barra; (4) el botón de tema no hacía nada si el
  navegador bloquea el almacenamiento — la elección vive también en memoria; (5) "← All agents"
  perdía el foco al venir de la Timeline. (3)–(5) verificados en Chrome.

## F7 — Auditoría de seguridad (02-10-2026)

Auditoría completa (skill security-audit, perfil standard, sobre 07ac45f; informe fuera del repo).
Sin sandbox en este Windows: revisión de código + experimentos solo con la stdlib.

- **costUSD no finito (bug de robustez, no de seguridad).** `json.loads` acepta `NaN`,
  `Infinity` y `1e999`; el parser aceptaba cualquier float en `cost-state`. NaN: `INSERT OR
  IGNORE` también salta NOT NULL, así que la fila se perdía en silencio (no congelaba la
  ingesta, como suponía el cazador). Infinity: se guardaba, `internal_cost` daba `share` =
  inf/inf = NaN y `/api/overview` respondía 500 en cada petición (`allow_nan=False`). Dos
  1e308 finitos también suman inf. Fix: solo `0 <= costUSD < 1e9` (`MAX_COST`); si no queda
  ninguno, la línea es unknown (visible en salud). Test; PARSER 15 (reconstruye la caché).
  Seguridad: rechazado — esas líneas las escribe Claude Code (Node: `JSON.stringify` no emite
  NaN/Infinity); inyectarlas exige escribir en `~/.claude`, que ya da más que un 500.
- **Primera vez que se sube `PARSER_VERSION` sin cambio de esquema** (14 → 15): el parser
  descarta ahora valores que antes guardaba, así que una caché vieja podía tener un `inf`. La
  versión del parser sirve para eso, no solo para el esquema: cambia lo que se lee → reconstruir.
- **Puerto reservado una sola vez y `run` por arranque** (mejoras de la auditoría, no hallazgos
  confirmados). Tests: otro socket no puede ocupar el puerto mientras uvicorn arranca; dos
  `create_app` → `run` distintos, y `hello` lo lleva. Probado en Chrome: parar traza, arrancar
  otro → la pestaña vieja pasa a "traza stopped · reload the page" y no pide nada más; F5 →
  "Watching". Es sobre todo un bug de UX: la pestaña obedecía a cualquier servidor del 7420.
- **Resto del informe a v2:** permisos 0700/0600 en POSIX, `Sec-Fetch-Site`, límite de
  suscriptores SSE, contención del glob del watcher, tokens negativos, dependencias fijadas.

## Antes de F7 — Timeline legible (02-10-2026)

- **Diagnóstico (revisor):** no era estética sino codificación de datos. Medido en la sesión de
  este proyecto: 42 agentes, 5,2 h activas, **2.559 marcas** (2.035 del principal); a ~500 px,
  ~37 s por píxel: un peine. Los "racimos con huecos" no eran pausas sin quitar (el eje sí las
  quitaba): las barras por turno solo pintaban modelo y herramienta, y el tiempo esperándote
  (< 5 min) quedaba vacío.
- **Hecho:** filas iguales colapsadas (42 → 4 filas en esa sesión), una barra continua por agente
  (86 tramos en el principal tras agrupar a 1,5 px), pausas marcadas solo en el eje. Probado en
  Chrome: escritorio y móvil (390 px, sin scroll horizontal), temas claro y oscuro, desplegar y
  plegar un grupo (los miembros salen por su descripción), clic en la barra → turno.
- **Dos bugs de datos encontrados al cuadrar la barra con el desglose (tests):**
  1. Una llamada sin resultado (interrumpida) seguida de un prompt horas después contaba esas
     horas como "herramienta": el desglose del principal sumaba **~10 h en 5,2 h activas**
     (herramientas 5 h 26 m). Ahora "en marcha" exige el resultado, como en el eje
     (`timeline.running`, que también usa `views._busy`): herramientas 1 h 24 m.
  2. Eventos que retroceden en el orden del fichero (100 → 50 → 120) contaban 50–100 dos veces.
     Ahora cada hueco cuenta desde el instante más tardío visto. Resultado: modelo 3 h 02 m +
     herramientas 1 h 24 m + tú 46 m = 5 h 12 m de 5 h 17 m activas, y la barra continua suma
     exactamente lo mismo que el desglose. El "tiempo activo" del resumen de agente (suma del
     desglose) también cambia; las cifras de desglose de F6.5 se han recalculado o borrado.
- **Mismo patrón en otros sitios (revisado, con test):** estado en vivo (`tool_in_flight`) y
  señal de colgada exigen una herramienta de la última respuesta **sin prompt posterior**: no
  tenían el agujero. Test nuevo con "herramienta sin resultado → prompt 4 h después" que cubre
  estado, colgada, resumen del agente y timeline. Las barras por turno (que medían la duración
  de cada turno) ya no existen.
- **`/code-review` (antes del commit), 1 hallazgo, corregido con test:** la barra continua
  alargaba hasta el final del eje CUALQUIER herramienta sin resultado, aunque el agente hubiera
  seguido (reproducido: Bash a los 10 s, respuesta a los 30 s, subagente hasta 1000 s → 970 s de
  "herramienta" inventados, y la barra dejaba de cuadrar con el desglose). Ahora solo la de la
  última respuesta sin prompt posterior, la misma regla que el estado en vivo y la colgada.

## Antes de F7 — Flame (02-10-2026)

- **Tests:** 9 casos de layout en `node --test` (raíz sola, 1:1 sin y con coste propio, tres
  niveles, un solo hijo, profundidad excedida → un "N more", hermanos bajo el mínimo, huérfanos,
  sin precio) + invariantes sobre el árbol de la fixture (área terminal = 100 %, nada se sale de
  su padre, raíz = total) con y sin agregados. **Mutaciones: 7 de 8 detectadas**; la que
  sobrevivió (colgar un huérfano de su `parent`) era una condición muerta: un huérfano no tiene
  `parent` por construcción (`db.orphans`; los 17 del disco, todos con `parent` nulo). Quitada.
- **Verificado en Chrome** (escritorio 740 px y móvil 302 px, claro y oscuro): la raíz coincide
  con el valor de la sesión en las tres probadas — 0d6a5565 (43 agentes) $165,03; e093c05a (40
  agentes) $372,33; 5a1f386d (25 agentes) $10,60. Rectángulos: 11, 6 y 27; render 1,8 ms, 0,5 ms
  y 2,4 ms. Clic y Enter abren el agente; al volver, foco en el mismo bloque; sin scroll
  horizontal en móvil.
- **Lo que enseña:** en 0d6a5565 y e093c05a el principal es el 90–96 % del valor (casi todo
  trabajo propio): los subagentes, juntos, son menos del 10 % y casi todos caen bajo 3 px. En
  5a1f386d, 25 subagentes llevan el 57 % y se distinguen uno a uno por su ancho.
- **Fallo visto y corregido:** la regla CSS global de iconos (`svg { width: 20px }`) encogía el
  flame a 20 px; el SVG del flame la anula.
- **Hallazgo (lo que el Flame descubrió, no solo respondió): en las sesiones con muchos
  subagentes, el hilo principal se lleva casi todo el valor.** Valor a precios de la API, 02-10-2026
  (0d6a5565 es esta misma sesión, aún en curso: $165,03 en la verificación en Chrome, $166,22 al
  hacer la tabla):

  | Sesión | Agentes | Valor | Principal (trabajo propio) | 42/39/24 subagentes juntos | Subagente mayor |
  |---|---|---|---|---|---|
  | 0d6a5565 | 43 | $166,22 | $149,92 — **90,2 %** | $16,30 — 9,8 % | $0,92 — 0,6 % |
  | e093c05a | 40 | $372,33 | $357,66 — **96,1 %** | $14,66 — 3,9 % | $1,92 — 0,5 % |
  | 5a1f386d | 25 | $10,60 | $4,60 — 43,4 % | $6,00 — **56,6 %** | $0,41 — 3,9 % |

  Lanzar 40 subagentes no es lo caro: lo caro es el contexto largo del hilo principal (en
  0d6a5565, el valor del principal es 49 % lecturas de caché, 35 % escrituras de caché y 15 %
  salida: el 84 % es mover su contexto, no generar). En
  5a1f386d (una tarea repartida en 24 subagentes cortos) sí pesan los subagentes. Consecuencia
  de diseño: en las sesiones grandes el "N more" es el caso normal (11 rectángulos para 43
  agentes; 6 para 40) → el zoom sube al primer puesto de v2 (plan). Para el GIF, 5a1f386d es la
  sesión donde el Flame hace lo que promete.
- **`/code-review` del Flame, 2 hallazgos, corregidos:** (1) la barra de la sesión salía coral
  (`.fl-d0` pisaba a `.fl-root`, misma especificidad) → neutra; (2) un agente en un ciclo de
  padres (él mismo, o dos que se apuntan) no se alcanzaba desde la sesión y su valor
  desaparecía del total sin aviso (la cabecera decía $10, el flame $5). El backend ya se protege
  de ciclos de un `meta.json` manipulado (`views.subtree`); el layout ahora recorre sin repetir y
  cuelga de la sesión lo que no alcanza. Test con autociclo y ciclo de dos.

## F7 — GIF con datos de demo (02-10-2026)

- **Generador** (`tools/make_demo_data.py`, determinista salvo las horas): 5 sesiones en 4
  proyectos inventados; la principal imita la forma de 5a1f386d — 25 agentes, $7,99, el
  principal 44 % y los subagentes 56 % ($0,04–$0,37 cada uno) — para que el Flame muestre bloques
  distinguibles. Leído por traza: 0 líneas desconocidas, 0 sin precio.
- **Fallo del generador visto en la primera grabación:** salían avisos de bucle en 3 sesiones.
  Eran inventados por azar (la misma llamada tres veces seguidas); un aviso falso en el GIF
  sería mentir. Ahora dos llamadas seguidas nunca son iguales: 0 señales.
- **La cabecera de la tabla se perdía** al hacer scroll hasta las filas nuevas (columnas de
  números sin título). Una cabecera fija no funciona aquí (la tabla no es el contenedor que hace
  scroll vertical) y no se reestructura el Tree por un GIF: el guion ordena por "Own value" y
  el subagente nuevo **sube** mientras gasta ($0,34 y cuarto a los 5 s), con la cabecera a la vista.
- **Verificado en el render:** la barra de la sesión del Flame sale neutra (el arreglo de CSS
  que no se había podido ver con Chrome desconectado).
- **Barra de salud del GIF:** con el primer generador decía "0 lines ignored", y un revisor
  podría pensar que el panel descarta datos sin decirlo. El generador escribe ahora las líneas que
  Claude Code escribe y traza ignora adrede (`attachment` de hooks, `file-history-snapshot`,
  `queue-operation`, `system:stop_hook_summary`; en disco real `attachment` es el tipo más
  frecuente): "0 unknown · 112 ignored (4 types)". Regrabado: 13,8 s, 3,9 MB.
- **Playwright no es dependencia de traza:** extra opcional `demo` en pyproject; `pipx install`
  no lo descarga. ffmpeg, aparte (no es de pip).

## F7 — cifras para el README (02-10-2026)

Regeneradas con el código actual (PARSER 15), reconstruyendo la caché en frío en una BD temporal:
- **Disco:** 65 sesiones, 196 ficheros (131 subagentes), 111.586 líneas, 9.655 peticiones.
  Reconstrucción en frío: 3,24 s el escaneo (3,5 s en total).
- **Valor a precios de la API de todo el disco:** $1.158,48 (crece en vivo; la cifra de F4,
  $983,43, ya no es la actual). Hoy: $84,23.
- **Salud:** 0 líneas desconocidas; 70.033 ignoradas adrede (13 tipos; `attachment` 50.762);
  466 peticiones con `output_tokens` implausiblemente bajos (aviso, no corrección); coste interno
  medido **al menos** $0,09 en 3 sesiones (0,12 % de lo que registran los `cost-state`).
- **Oráculo** (`pytest -s tests/test_oracle.py`): cuadran **exacto, tokens y dólares**, las dos
  sesiones sanas (36b96010: 23.334 tokens de salida, $2,012743; 598796c2: 371, $0,149508). No
  cuadran dos: 3416476c (copia, known issue) y 7bc000bb, donde traza cuenta más que el proceso
  (92.647 frente a 50.328 tokens de salida de Opus; $39,85 frente a $16,89).
- **7bc000bb, medido (revisión):** descartadas, con datos, (2) subagentes atribuidos al principal
  — no tiene carpeta `subagents/`; la deduplicación — 104 `requestId` en la ventana, ninguno
  repetido con `output_tokens` distinto; (3) la ventana — no hay líneas tras el último
  `cost-state`. Tiene dos `cost-state` con el **mismo** `startTime`: el primero, vacío, justo tras
  el `custom-title` (el renombrado "(fork)"), y todas las peticiones van después. Sin huella de un
  segundo escritor (una sola versión 2.1.286 y `claude-vscode`, ningún timestamp que retroceda).
  El proceso de VS Code estuvo abierto 26 h con dos huecos (5,2 h y 18 h); por tramos, 29.890 +
  12.429 + **50.328**: el último tramo es **exactamente** lo que dice `cost-state`, y en dólares
  $16,893056 frente a $16,893056. **Conclusión:** el contador del proceso se reinició tras la
  pausa de 18 h sin cambiar su `startTime`; traza cuenta bien, el supuesto del oráculo ("desde
  `startTime`") es el que falla. Contando desde el reinicio, 3 de 4 cuadran exacto. El test no se
  cambia: una regla para detectar reinicios sacada de un solo caso sería inventar.
- **Subagentes sin fin registrado, re-medido** (misma caché recién reconstruida, `agent_tree` de las
  65 sesiones): 128 de 131 subagentes "done" (109 normales + 19 huérfanos de skill fork); los 3
  restantes son los mismos 3 de siempre, los tres en e093c05a, en "idle". (El "3 de 93" de F4 era
  con menos subagentes en disco.)
- **Instalación en limpio** (venv nuevo, `pip install .`): el paquete trae los 5 estáticos
  (incl. `flame.js`) y `prices.toml`, crea `traza`, y servido así responde `/`, los estáticos y
  la API con datos. [sin verificar] `pipx install git+https://…`: el repo aún no está publicado.
- **CLI en inglés** (`traza scan`, `traza report` y su texto de uso): son superficie pública, como
  el README. Los docs internos (design, plan, findings) siguen en español: memoria del proyecto.

## F7 — README, segunda revisión (03-10-2026)

- **Cifras re-medidas** (caché reconstruida en frío): 112.692 líneas, 9.722 peticiones, 132
  subagentes (129 "done", los mismos 3 "idle"), 467 peticiones implausibles, coste interno al
  menos $0,09 en 3 sesiones. Escaneo en frío: 6,16 s (ayer 3,24 s con 111.586 líneas: varía con
  el estado del disco) → el README dice "3–6 s". El disco crece: el README fecha las cifras.
- **Líneas ignoradas: 70.809 de 112.692 (63 %).** Salida de hooks (`attachment` `hook_success`
  27.686, `async_hook_response` 8.762, `hook_additional_context` 230) = 52 % de lo ignorado; el
  resto, recordatorios y metadatos de sesión (`total_tokens_reminder`, `last-prompt`,
  `atis-latch`, `mode`, `bridge-session`…).
- **`test_oracle` en un repo clonado sin logs** (HOME vacío): se salta con su motivo; suite
  211 + 1 saltado. Es ejecutable sin Claude Code.
- **Captura estática** `docs/screenshot.png` (51 KB, datos de demo) generada por
  `tools/record_demo.py` junto al GIF.

## F7 — impeccable audit + critique (03-10-2026)

- **Audit (técnico) 16/20.** Contraste 0 fallos en 4 vistas × escritorio/móvil × claro/oscuro;
  0 desbordamientos; 0 controles sin nombre; detector 0 hallazgos en el código. Fallos: selección
  de texto en oscuro 1,15:1 (color escrito a mano); objetivos táctiles < 24 px en móvil (P2).
- **Critique (diseño) 25/40**, dos evaluaciones aisladas (diseño / detector+navegador). El
  detector sobre la página renderizada da ~30 avisos, casi todos falsos positivos (texto dentro de
  `<details>` cerrados conserva su caja); el escaneo por URL no puede correr con traza (la
  conexión SSE nunca deja la red en reposo; la herramienta lo informa como "limpio").
- **Arreglado (5 pasos, cada uno verificado con Playwright en escritorio/móvil y claro/oscuro):**
  1. barra "Where the value goes" proporcional también con total < $1 (`flex-grow` que suma < 1
     solo reparte esa fracción: a $0,09 la barra se llenaba ~9 %); selección y scrollbar como
     tokens; `title` en el título en vivo;
  2. el panel deja de ser región viva (se rehacía en cada tick); una línea de estado para lectores
     de pantalla (accesibilidad por completitud: tooltips sin cambiar, sin prueba con NVDA);
  3. vista de juicio con tarea | resultado lado a lado; "Cost & time" plegado;
  4. árbol ordenado por valor incl. subagentes por defecto; herramientas en ámbar frente al morado
     del modelo; gama propia para las partes del valor; leyenda del Flame con los agentes sin
     nombre visible (al lado en panel ancho, acordeón en estrecho).
     **La primera versión de la leyenda ("solo los de N more") salía vacía en escritorio**: el
     anonimato venía de no tener etiqueta, no de estar agregado. Se paró y se cambió;
  5. GIF y captura regrabados. Al regrabar: las flechas ↕ en cada cabecera desbordaban la tabla
     con la pastilla "Running tool" → subrayado punteado (sin ancho).

## F7 — critique, segunda pasada y cabecera de jerarquía (03-10-2026)

- **Re-lanzado tras los 5 pasos: 25 → 26/40.** Detector sin hallazgos nuevos; todos los colores
  nuevos ≥ 3:1 en los dos temas. Encontró un fallo antiguo (foco perdido al abrir un agente:
  carrera con los ticks; el primer arreglo con `.then()` fallaba 1 de 8 arranques, el definitivo
  enfoca desde la llamada que muestra la vista: 12 de 12) y una regresión de la pasada de color
  (Input y lecturas de caché el mismo gris, 1,04:1).
- **Abiertos arreglados:** descripción como titular del agente (árbol, vista, aria-label); en
  móvil, desplazar al panel tras pintarlo (el primer intento solo bajaba 246 px: la página aún
  era corta); Output en frambuesa (no el coral de error); texto pequeño esencial a `--ink-2`;
  tarjeta negra visible en oscuro (1,29 → 1,7:1 frente al lienzo); flecha de orden al pasar,
  fuera del flujo (ancho de la tabla 660 px antes y durante).
- **Flame → cabecera de jerarquía** (decisión del autor con el Flame a ancho completo delante):
  en la demo, 2 filas — "Main agent · $7.86" y "24 subagents · 57.1 %" | "own work · 42.9 %" —
  y la lista de 24 debajo. En la sesión real 0d6a5565 el trabajo propio del principal es el
  93,9 %: los subagentes (6 %) y su anidación quedan por debajo de lo legible a este ancho; es el
  dato, no un fallo.

## F7 — notas de la revisión de F5, medidas (03-10-2026)

1. **"Load earlier" (arreglado).** En el principal de 0d6a5565 (1.260 turnos): cargar todo eran
   **25 clics y 36 s**, sin forma de volver a los últimos 50 (al reabrir el agente sí vuelve: bien).
   Ahora "Load all" (una petición `start_at=0`: 0,86 s; la API tarda 0,69 s y manda 592 KB por
   1.394 elementos) y "Show only the latest 50" (0,74 s).
2. **Salto de scroll (no hay).** Demo en vivo, todos los turnos cargados, vista anclada en un turno
   a mitad de lista: llegan ticks (52 → 53 turnos, la lista se reconstruye) y el turno sigue a
   198 px. Los turnos nuevos van al final y el panel conserva su posición.
3. **Arranque en frío (medido; problema de copia).** Demo, 1.018 líneas: panel con sesiones a
   1,1 s. Disco real, ~113.000 líneas: el servidor responde a 1,05 s pero las sesiones llegan a
   **10,1 s** (con otra instancia de traza leyendo los mismos ficheros a la vez; el escaneo solo,
   3,2–6,2 s). **Mientras tanto el panel dice "No sessions on disk yet" y "0 of 0"** con 65
   sesiones en disco: es falso, y es la primera pantalla de quien lo instala. Pendiente de decidir.
4. **Render con salidas recortadas a 20.000 caracteres (medido).** Principal de 0d541f67 (líneas de
   hasta 1,46 MB): desplegar con clics reales los 57 turnos visibles y pintar todo el texto, 3,5 s
   (119 bloques, 3 recortados). Sin acción.
5. **"The agent finished without writing any text" (visto; engaña en un caso real).** Sale en
   pantalla como se diseñó. Pero los 10 casos del disco son subagentes que **sí** devolvieron un
   informe, con la herramienta `SubagentHandback` en su último turno en vez de con texto: el
   mensaje es literalmente cierto y hace creer que no devolvieron nada. Pendiente de decidir.
- **Notas 3 y 5, arregladas (decisión del autor):** (3) `scanning` en `/api/overview` hasta el
  primer tick y "Reading your Claude Code logs… The first start builds the cache" en el panel;
  verificado en frío con el disco real (las 65 sesiones aparecen solas al terminar, sin recargar).
  (5) el resultado de un subagente que entrega con `SubagentHandback` es ese informe, leído como
  texto; verificado con el crítico de cobertura de la auditoría (7.546 caracteres). Tests de los dos.

## F7 — /code-review de F7 (07ac45f..HEAD), 3 hallazgos, corregidos (03-10-2026)

1. **Timeline:** una llamada sin respuesta en la última respuesta de un agente se pintaba abierta
   hasta el final del eje común aunque el agente estuviera parado (otro agente alargaba el eje):
   horas de "herramienta" inventadas que el desglose no tenía. Ahora `segments(..., open_tool)` y
   `session_timeline` se lo dice con el estado en vivo (§7.1). Test.
2. **Generador de demo:** borraba sin mirar la carpeta indicada (`make_demo_data.py .` se llevaba
   el repositorio). Visto en la verificación: un primer intento, con el Edit bloqueado, borró la
   carpeta temporal de prueba con su `notes.txt`. Ahora solo borra si contiene lo que él genera.
3. **Pestaña parada:** cada refresco y clic dejaban un error sin capturar en la consola; un
   manejador ignora ese rechazo concreto. Verificado: 0 errores tras clic con "traza stopped".

## F7 — instalación limpia (03-10-2026)

Desde un directorio temporal sin nada de traza y con `~/.traza/` renombrado aparte: venv nuevo +
`pip install <repo>` (pipx y uv no están instalados; un venv propio es lo que pipx hace por dentro).
- `traza --help` funciona; `traza serve --no-open` arranca sin `~/.traza/` y lo crea
  (`traza.db` + `-wal`/`-shm`).
- `/api/overview` respondió `scanning: true` y luego listó las sesiones a los 4,3 s (65 sesiones).
- `/`, `app.js`, `flame.js`, `styles.css` → 200 (los estáticos van dentro del paquete instalado).
- En navegador (Playwright): lista, sesión, pestañas Trace y Flame; conexión "Watching"; 0 errores JS.
- Restaurado el `~/.traza` original; la caché de prueba queda en `~/.traza.clean-test` (borrable).

Falta: `pipx install git+https://github.com/LanderIglesias/traza` real, cuando el repo esté publicado.
