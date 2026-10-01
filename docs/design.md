# traza — Documento de diseño

> Estado: **borrador pendiente de aprobación** · Fecha: 2026-09-29
> Todo lo marcado como **[verificado]** se ha comprobado en la documentación oficial
> (code.claude.com/docs) o en los ficheros reales de `~/.claude/` de esta máquina.
> Lo marcado como **[sin verificar]** es una hipótesis y tiene una tarea asignada en `plan.md`.

---

## 1. Objetivo

Un panel web **local** que muestra, en tiempo real y sobre el historial reciente,
qué hacen por dentro las sesiones de Claude Code:

- qué agentes y subagentes existen, quién lanzó a quién y en qué estado están;
- cuántos tokens y cuánto **coste estimado** consume cada petición, agente, sesión y modelo;
- qué herramientas usa cada agente, con qué argumentos y qué resultado obtiene;
- **si un agente está trabajando bien**: su encargo y lo que devolvió, lado a lado,
  y señales automáticas deterministas (errores, bucles, reintentos fallidos, herramientas colgadas).

**Ángulo diferencial frente a Agent View / claude-code-tracer y similares:** no es solo
"cuánto gasté", es una **herramienta para juzgar la calidad del trabajo de cada subagente**,
con **exactitud demostrada** (los tokens cuadran al token con el cálculo propio de Claude Code,
ver §8) y honesta sobre lo que no sabe (`?` en vez de cifras inventadas).

## 2. Usuarios

- **Usuario principal:** el autor, a diario, mientras trabaja con Claude Code (sobre todo desde
  la extensión de VS Code) y lanza muchos subagentes. Necesita ver coste por subagente y juzgar
  si su output es bueno.
- **Usuario secundario:** reclutador técnico / revisor de portfolio. Necesita entenderlo en 30 s
  (README + GIF) e instalarlo con un comando.

## 3. Fuera de alcance (decisiones explícitas)

- **No** muestra el razonamiento interno del modelo: los bloques `thinking` se guardan en disco
  **con el texto vacío** [verificado: 604 de 604 vacíos en una sesión]. traza muestra acciones y
  resultados, no pensamientos.
- **No** es un "agent OS": no lanza agentes, no tiene memoria, scheduling ni permisos.
  Es la capa de observabilidad.
- **No** conserva historial más allá de lo que Claude Code mantiene en disco (ver §6.4).
- **No** es multiusuario ni accesible desde la red.

## 4. Fuentes de datos

### 4.1 Elegida para v1: ficheros JSONL de sesión

[verificado en disco]
- Sesión principal: `~/.claude/projects/<proyecto>/<sessionId>.jsonl`.
- Subagentes: `~/.claude/projects/<proyecto>/<sessionId>/subagents/agent-<id>.jsonl` +
  `agent-<id>.meta.json` con `agentType`, `description`, `toolUseId` (la llamada del padre que lo
  lanzó) y `spawnDepth`.
- Cada línea `assistant` trae `message.model`, `message.usage` (input, output, cache read, cache
  write separada en 5 min / 1 h, thinking tokens), `stop_reason`, `requestId` y `uuid`.

**Por qué:** cero configuración (no toca `settings.json`), da el historial gratis (sesión viva y
antigua se leen con el mismo código) y es la única fuente con el **contenido completo** (encargos,
argumentos, resultados), que es lo que hace falta para juzgar calidad.

**Qué se pierde:**
- Es un formato **interno, no documentado**; una actualización puede romper el parser
  (mitigación: parser tolerante, tipos desconocidos contados y visibles, tests sobre fixtures).
- El estado "esperando tu permiso" no es visible.
- El estado "pensando" solo se deduce del `mtime` del fichero: las líneas de una respuesta se
  escriben **todas de golpe al terminarla**, no en streaming [verificado en vivo, `findings.md`
  §1]. Su `timestamp` es el de generación de cada bloque, no el de escritura.
- El campo `apiBlockIndex` solo existe en algunas versiones (sí en la extensión 2.1.267, no en la
  CLI 2.1.226) [verificado]: el parser no depende de él.
- Las llamadas internas de Claude Code (p. ej. a Haiku para títulos) **no aparecen** en el JSONL
  [verificado: 15.777 tokens de Haiku en `cost-state`, 0 en el JSONL]. Se muestran como
  "coste interno no desglosado" cuando se puede calcular la diferencia.

### 4.2 Opcional v1.x: hooks HTTP

[verificado en docs] Existen hooks `type: "http"` que hacen POST del JSON del evento a una URL;
`PreToolUse`/`PostToolUse` se disparan también dentro de subagentes con `agent_id`/`agent_type`.
Aportarían estado exacto al instante ("necesita input" vía `Notification`). Solo se implementan si
tras usar v1 se echa de menos. Requiere modificar `settings.json` con instalador/desinstalador.
[sin verificar] qué hace Claude Code si el servidor del hook está caído (¿espera al timeout?).

### 4.3 Descartadas

- **OpenTelemetry:** necesita receptor OTLP; su `cost_usd` también es "estimated cost" [verificado
  en docs]; identifica subagentes por nombre, no por id, así que el cruce con el árbol es frágil.
  Aporta poco sobre el JSONL.
- **Proxy de la API:** intercepta tráfico con credenciales; riesgo desproporcionado.
- **Escaneo de procesos:** no da información de agentes ni tokens.

## 5. Arquitectura

```
~/.claude/projects/**/*.jsonl ──(1) watcher──▶ (2) parser ──▶ (3) SQLite (caché)
                                                                   │
                     Navegador ◀── SSE ── (4) FastAPI ◀────────────┘
                   (5) HTML + CSS + JS modules
```

1. **Watcher** — tarea asíncrona dentro de FastAPI (`watcher.watch`). **Invariante: el event loop
   nunca se bloquea.** Cada tick (`scan`) corre entero en `asyncio.to_thread` con **una conexión
   propia**: la espera por cerrojo (hasta 30 s si `python -m traza.scan` está escribiendo) solo
   detiene a ese hilo; heartbeats SSE y peticiones HTTP siguen atendiéndose. Si el cerrojo no se
   libera en 30 s, se pierde ese tick y se reintenta en el siguiente. Los endpoints de lectura
   son funciones síncronas (`def`), que FastAPI ya ejecuta en su pool de hilos; con WAL los
   lectores no esperan al escritor. [test: `test_watch_loop.py` mide que el loop no se retrasa
   más de 0,1 s con el cerrojo tomado por otra conexión; con la versión ingenua (scan dentro del
   loop) el mismo test se bloquea 30 s y falla]. Cada 0,5 s:
   - dos `glob` para ficheros nuevos: `projects/*/*.jsonl` (sesiones principales) y
     `projects/*/*/subagents/*.jsonl` (subagentes). No se usa `**` para no recorrer carpetas
     que no contienen transcripts;
   - `os.stat()` de cada fichero registrado; si `(mtime, size)` no cambió, no lo abre;
   - lee desde el offset guardado **solo líneas completas** (terminadas en `\n`); una línea a
     medias no se consume: el offset se queda al principio de ella y se relee en el siguiente
     tick (sin buffer en memoria, sin estado extra);
   - `size < offset`, o la **huella de la primera línea** (`files.head`) cambió → truncado o
     sustituido: borra los datos del fichero y reprocesa desde 0;
   - un fichero que desaparece o está bloqueado entre el `stat` y la lectura no tumba el tick:
     borrado en cascada o reintento en el siguiente;
   - **ningún valor del JSON llega a SQLite sin normalizar**: el parser solo emite texto, enteros
     de 64 bits o `None` (un dict o un entero gigante haría fallar el `INSERT`, se desharía el
     tick entero y se repetiría para siempre);
   - `FileNotFoundError` → borrado en cascada de sus datos + evento SSE de borrado;
   - la carga inicial va a `asyncio.to_thread` con **su propia conexión** SQLite [medido en F2:
     todo el disco, 146 ficheros y 89k líneas, **1,71–1,74 s** en tres construcciones seguidas
     (una medida aislada de 4,1 s no se repitió); el 60 % es decodificar JSON; el mayor fichero,
     71 MB, 0,56 s]. Solo pasa al crear la caché: después cada tick lee solo lo nuevo (0,01 s).
   - Una línea que nunca se completa (proceso muerto a mitad de escritura) cuesta un `stat` por
     tick, no una lectura: el tamaño guardado incluye los bytes incompletos, así que
     `(size, mtime)` no cambia y el fichero ni se abre [test].
   - Coste a vigilar: N `stat()` cada 0,5 s (antivirus en Windows). Se mide si la carpeta crece.
2. **Parser** — funciones puras, sin I/O ni dependencias: línea JSON → registros para `requests`,
   `events`, `agents`, `sessions`. Es la pieza con más tests.
3. **SQLite** — caché desechable (ver §6.4), modo WAL, escrituras de un tick en **una transacción**
   (`executemany`) que empieza con `BEGIN IMMEDIATE` **antes** de leer los offsets: si corren dos
   escáneres a la vez (servidor + `python -m traza.scan`), el segundo espera (hasta 30 s) y ve lo
   que escribió el primero, en vez de trabajar con offsets viejos. Los lectores no esperan (WAL).
   La raíz se normaliza con `Path.resolve()`: ruta absoluta, **enlaces simbólicos resueltos**
   (equivale a `realpath`) y, en Windows, las **mayúsculas reales** del disco [verificado: la
   ruta en minúsculas y en mayúsculas resuelven a la misma; test]. Todas las rutas guardadas
   salen de un `glob` sobre esa raíz, así que no puede haber dos grafías del mismo fichero.
   Un `meta.json` que aún no existe o está a medio escribir se **reintenta cada tick**
   (`agents.meta_read = 0`) aunque el `.jsonl` ya no cambie.
4. **FastAPI** — sirve la página, la API JSON y un endpoint SSE.
5. **Frontend** — HTML + CSS + JavaScript con `<script type="module">` nativo, sin framework ni
   build. Justificación: árbol + timeline + contadores no necesitan React; así la instalación es
   solo Python.

**Por qué SSE y no WebSockets:** el flujo es unidireccional (servidor → navegador) y `EventSource`
se reconecta solo.

## 6. Modelo de datos

### 6.1 Definiciones

- **Sesión** = un `sessionId` = un fichero `<proyecto>/<sessionId>.jsonl`.
- **Agente** = el hilo principal de la sesión (`agent_id = "main"`) o un subagente.
- **Jerarquía** = el `toolUseId` del `meta.json` del subagente se busca entre los `tool_use` de
  los ficheros de la sesión; el agente que lo contiene es el padre. Funciona a cualquier
  `spawnDepth`. Si no se encuentra → **huérfano**, se muestra como raíz, con uno de dos motivos
  (`db.orphans`, calculado al consultar como el padre):
  - `sin_tool_use_id`: su `meta.json` **leído completo** no dice quién lo lanzó; **nunca** tendrá
    padre. En disco, 8 de 90 subagentes, **todos** forks de la skill `/code-review` [verificado].
    Un `meta.json` que aún no se ha podido leer cuenta como `padre_no_encontrado` (puede llegar).
    Su `meta.json` no dice qué skill fue (`{"agentType": "general-purpose", "spawnDepth": 1}`):
    lo que la identifica es el principio de su encargo ("Review target: …"). F4 muestra
    "fork de skill" + ese principio, no "padre desconocido". UI: "lanzado
    fuera de la herramienta Agent".
    **Decisión F4 — no se enlazan con su padre.** El `tool_result` de la skill trae el `agentId`
    del fork (`toolUseResult.status = "forked"`, 11 en disco), así que el enlace es posible;
    pero ese resultado llega cuando el fork **termina**: el nodo saltaría de raíz a hijo a mitad
    de sesión. Un fork que se queda como raíz con la etiqueta "skill fork" es más claro que un
    nodo que cambia de sitio.
  - `padre_no_encontrado`: dice quién lo lanzó, pero ese `tool_use` no está en la sesión (0 en
    disco). UI: "padre desconocido".
- **Huérfano temporal en vivo — decisión: no se añade espera en la UI.** El `tool_use` que lanza
  un subagente se escribe al terminar la respuesta del padre, es decir **antes** de que la
  herramienta se ejecute y exista el fichero del subagente (`findings.md` §1), y en un mismo
  tick el fichero principal se procesa antes que sus subagentes (orden de ruta). Así que el
  "salto" de raíz a hijo no debería verse. [sin verificar en vivo] → se comprueba en F4; si
  aparece, la UI retrasa un tick los huérfanos `padre_no_encontrado` recientes.
- `claude --resume` sigue escribiendo en el **mismo fichero** [verificado en vivo,
  `findings.md` §2].

### 6.2 Tablas

| Tabla | Clave | Campos principales |
|---|---|---|
| `meta` | `key` | `cache_generation` (uuid al crear la BD), `parser_version` |
| `files` | `path` | `session_id`, `agent_id` (`main` para el fichero principal; el id del subagente para `subagents/agent-<id>.jsonl`), `size`, `mtime_ns`, `offset`, `first_ts` (inicio de sesión), `head` (huella de la primera línea) |
| `sessions` | `session_id` | proyecto, `cwd`, título (`ai-title`/`custom-title`), inicio, última actividad |
| `agents` | `(session_id, agent_id)` | `parent_tool_use_id`, `agent_type`, `description`, `spawn_depth`. El **padre se deriva** al consultar (§6.1): no depende del orden de ingesta |
| `requests` | `request_id` | hora, modelo, `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h`, `speed`, `inference_geo`, `web_search_requests`, `stop_reason` |
| `request_refs` | `(request_id, file_path)` | `session_id`, `agent_id`: qué ficheros contienen líneas de cada petición (también las que solo tienen `thinking` y no crean evento) |
| `ignored` | `(file_path, type)` | `n`: contador de líneas ignoradas por tipo, para la barra de salud |
| `events` | `id INTEGER PRIMARY KEY AUTOINCREMENT` | `(file_path, byte_offset, block)` UNIQUE, `uuid` (solo verificación), `session_id`, `agent_id`, hora, `kind`, `tool_name`, `tool_use_id`, `is_error`, `denial`, `input_hash`, `origin`, `request_id`, `file_path`, `byte_offset`, `length` |

Índices: `events(agent_id, id)`, `events(tool_use_id)`, `events(uuid)`, `events(request_id)`,
`events(file_path)`, `request_refs(file_path)`, `request_refs(session_id)`.
Vista `request_owner(request_id, owner_session_id, owner_agent_id)`: la dueña **se calcula**,
no se guarda (ver "Sesión dueña").

**Claves (decisiones con datos):**
- **Idempotencia de `events` por posición, no por `uuid`:** `(file_path, byte_offset, block)`.
  El `uuid` **no es único** [verificado]: dentro de `3416476c` hay 16.878 `uuid` y solo 16.874
  distintos (4 repetidos con contenido distinto, todos `attachment`). La posición siempre es
  única, existe para cualquier línea (lleve `uuid` o no) y es estable porque los JSONL solo
  crecen. Al detectar truncado se **borran primero los eventos de ese fichero** y se reprocesa
  desde 0, así que las posiciones viejas nunca conviven con las nuevas. El `uuid` se guarda
  (con índice) solo para verificar el contenido bajo demanda (§6.5).
- `requests.request_id` PK **global**, a propósito **no** compuesta con `session_id`: 1.274
  `requestId` aparecen en dos sesiones [verificado], todos entre `7bc000bb` y `3416476c`. La
  segunda es una **copia** de la primera: comparte 0 `uuid` (los regenera), el `usage` es
  idéntico en las 3.407 líneas copiadas y 1.273 de las 1.274 peticiones conservan su
  `timestamp` original [verificado]. Con clave compuesta el coste total contaría dos veces cada
  petición heredada; con clave global se cuenta una vez.
- **Sesión dueña** de una petición = la sesión que **empezó antes** (`timestamp` de la **primera
  línea del fichero principal, sea del tipo que sea**, también ignorada: la copia empieza con el
  mismo prompt y el mismo `timestamp` que la original, solo la primera línea las distingue
  [verificado en F2, `findings.md` §F2]; `7bc000bb` 14:42 frente a `3416476c` 14:47). Se calcula con la vista
  `request_owner` sobre `request_refs` + `files.first_ts` del fichero principal, así que **no
  depende del orden de ingesta** y no hay nada que actualizar. Reglas completas, cada una con
  test:
  1. gana el `first_ts` más antiguo (texto ISO-8601, orden lexicográfico);
  2. `first_ts` = hora de la **primera línea que la tenga**; si la primera es `unknown` o no trae
     hora, se usa la siguiente;
  3. una sesión sin **ninguna** hora (o sin fichero principal) va **la última**;
  4. empate exacto (copia manual, backup restaurado) → gana el `session_id` menor en orden
     lexicográfico, y después el `agent_id`. Nunca depende del orden del `glob`. En la copia, esos
  eventos se muestran como **heredados** (visibles, sin sumar coste).
- **Borrado y copias:** al desaparecer un fichero se borran sus eventos y sus `request_refs`; las
  peticiones que ya no tienen ninguna referencia se borran **en el mismo tick** (también al
  truncar), así que no se acumulan peticiones huérfanas [test]. La **reasignación es automática**:
  la vista elige la siguiente sesión viva más antigua. Así, cuando Claude Code borre la
  original a los 30 días, la copia recupera el coste de lo heredado en vez de quedarse a 0.
- **Coste heredado sin reparsear:** no hace falta un campo `inherited_from_session_id` en
  `requests`, porque una petición puede estar heredada por varias sesiones y una columna solo
  guarda una. Se deriva con una consulta: las peticiones heredadas por la sesión `S` son las que
  `S` referencia y cuya dueña es otra sesión.
  ```sql
  SELECT o.owner_session_id, COUNT(*)
  FROM (SELECT DISTINCT request_id FROM request_refs WHERE session_id = :S) r
  JOIN request_owner o USING (request_id)
  WHERE o.owner_session_id <> :S
  GROUP BY o.owner_session_id
  ```
  Con eso la UI puede pintar cualquiera de las tres presentaciones de §7.

### 6.3 Reglas de ingesta

- **Idempotencia:** clave `(file_path, byte_offset, block)` en `events` e `INSERT OR IGNORE`
  (ver "Claves" arriba). Reprocesar nunca duplica, incluidas las líneas sin `uuid`.
- **Tokens una vez por petición:** Claude Code parte cada respuesta en varias líneas (una por
  bloque) y **repite el `usage` completo en cada una** [verificado: 1.036 de 1.036 peticiones
  multilínea]. Sumar línea a línea duplica o triplica el coste. Por eso los tokens viven en
  `requests` (una fila por `requestId`, upsert: campos de la primera línea salvo `output`, que es el **mayor visto** porque crece entre las líneas de una misma respuesta, findings.md §F4) y **nunca**
  en `events`. El esquema impide reintroducir el bug.
- `assistant` sin `requestId` (7 en disco) → no crea fila en `requests`, se cuenta como visible.
  Sin `usage` (0 en disco) → tokens `NULL` ("no lo sé"), nunca 0.
- Modelo `<synthetic>` (mensajes generados por Claude Code, 18 en disco, **ninguno con tokens**)
  → precio explícito 0.
- **Tipos de línea** [verificado, lista real de este disco]:
  - *mostrados:* `user` (prompt o `tool_result`), `assistant` (texto, `tool_use`), `system`
    subtipos `api_error` y `compact_boundary`, `ai-title`/`custom-title` (título).
  - *conocidos e ignorados (contados):* `attachment` (casi todo salida de hooks),
    `queue-operation`, `last-prompt`, `mode`, `file-history-snapshot`, `file-history-delta`,
    `bridge-session`, `atis-latch`, `frame-link`, `artifact-comment-monitor`,
    `artifact-autoreact-ledger`, `cost-state`, resto de subtipos de `system`.
    `cost-state` **no se guarda en la BD**: el test oráculo lo lee directamente del JSONL.
  - *unknown:* todo lo demás, contado y visible.
  - Proporción ignorada hoy: **63 % de las líneas, 41 % de los bytes**. v1 muestra el trabajo del
    modelo, no toda la maquinaria de Claude Code; el README lo dice así.

**Correspondencia línea JSONL → `events.kind`:**

| Origen en el JSONL | `kind` |
|---|---|
| `user` sin ningún bloque `tool_result` (texto, o texto + imagen/documento) | `prompt`, **un evento por línea** (`block = 0`) + `origin` |
| `user`, bloque `tool_result` | `tool_result` (+ `is_error`, `tool_use_id`; si hay error, `denial` = `toolDenialKind` de la línea: la herramienta **no se ejecutó**) |
| `assistant`, bloque `text` | `text` |
| `assistant`, bloque `tool_use` | `tool_use` (+ `tool_name`, `tool_use_id`, `input_hash`) |
| `assistant`, bloque `thinking` | no crea evento (texto vacío en disco) |
| `system`, subtipo `api_error` | `api_error` |
| `system`, subtipo `compact_boundary` | `compact_boundary` |
| `ai-title` / `custom-title` | no crea evento; actualiza `sessions.title` |
| tipo o subtipo en la lista de ignorados | no crea evento; suma al contador de ignorados |
| cualquier otra cosa | `unknown` |

Una línea puede producir varios eventos (uno por bloque). **`block` es el índice del bloque en
el array `message.content`** (0, 1, 2…), no un contador por tipo: dos bloques `text` seguidos
tienen `block` distinto. Un mensaje `user` con varios `tool_result` (herramientas pedidas en
paralelo) produce **un evento por cada `tool_result`**, cada uno con su `block` y su
`tool_use_id`. Si `message.content` es un string (prompt de texto plano), `block = 0`.
En disco, los `tool_result` en paralelo llegan hoy en líneas separadas y solo 5 líneas
`assistant` tienen más de un bloque [verificado]; el parser admite ambos casos igualmente.
Un bloque de `assistant` de tipo no conocido → evento `unknown` en su `block`.

**`origin` de un `prompt`** [verificado en F1, recuento sobre 771 líneas `prompt` del disco]:
no todo `prompt` lo escribió el usuario. El parser guarda `origin` = `"meta"` si la línea trae
`isMeta: true`; si no, `origin.kind` tal cual; si no hay, NULL. Reglas que usa la UI:

| Pregunta | Regla | Evidencia en disco |
|---|---|---|
| ¿Lo escribió el usuario? | **solo** `origin = "human"` | 385 líneas (348 texto + 37 comandos `/…`) |
| ¿Es el **encargo** de un subagente? | el **primer** `prompt` de su fichero `agent-<id>.jsonl`, sea cual sea su `origin` (se decide por posición en F2) | 82 de 82 prompts sin `origin` en subagentes son el primero del fichero |
| Todo lo demás | inyectado por Claude Code; se muestra atenuado | `meta` 135 (skills, avisos), `task-notification` 94, `peer` 12, y en el hilo principal NULL 78: 37 prompts del SDK (`promptSource: "sdk"`), 28 `[Request interrupted by user]`, 13 resúmenes de compactación |

NULL en el hilo principal **no** significa "del usuario" ni "encargo".

Desde F3 el parser distingue además dos orígenes que el modelo **no contesta** (cuentan para el
autómata de estado, §7): `interrupted` (texto que empieza por "[Request interrupted") y
`local-command` (la línea **empieza** por `<local-command-stdout>` o `<command-name>`; un prompt
humano que solo las contiene, p. ej. texto pegado, sigue siendo `human`), con prioridad sobre
`isMeta`. En disco las 20 líneas que empiezan así tienen `origin` vacío, ninguna `human`.

### 6.4 Contrato de la caché

**La caché es desechable; la fuente de verdad son los JSONL.**
- Se puede borrar `~/.traza/traza.db` en cualquier momento y se reconstruye.
- traza solo borra un fichero de BD si es **suya** (tabla `meta` con `cache_generation`) o una BD
  SQLite **vacía**, entendida como **sin ninguna tabla** (creación interrumpida). Una caché con
  esquema pero sin filas (máquina sin sesiones) es válida y **no** se toca [test: se reabre con
  la misma generación]; una ruta `--db` que apunte a otra cosa da error y no se
  toca. La creación del esquema es una sola transacción: un Ctrl+C a medias deja una BD vacía.
- Si la versión cambia mientras otro proceso de traza tiene la BD abierta: en Windows, error claro
  ("en uso por otro proceso"); en POSIX el proceso viejo sigue escribiendo en un fichero ya
  borrado, sin efecto sobre la BD nueva.
- No hay migraciones de esquema: si cambia el esquema o `parser_version`, se borra y reconstruye.
- Espejo de lo que hay en disco: si Claude Code borra un JSONL, traza borra sus datos.
- [verificado en docs, *Data usage*] Claude Code guarda los transcripts **30 días** por defecto
  (`cleanupPeriodDays`); las sesiones de Claude Desktop/Cowork están exentas por defecto. La
  ventana es **rodante**: el panel pierde datos continuamente, y conviven dos regímenes de
  retención. Para más historial se sube `cleanupPeriodDays`.
- Si algún día se quisiera historial propio (copiar JSONL), la caché pasaría a ser un archivo
  con migraciones: **es otro producto**, no una opción.

### 6.5 Contenido bajo demanda

El contenido pesado (argumentos y resultados de herramientas, textos) **no se copia** a SQLite.
Cada evento guarda `(file_path, byte_offset, length)`; al desplegarlo, `GET /content?ids=…`
agrupa por fichero, ordena offsets y lee. Se verifica que el `uuid` de la línea leída coincide;
si no (fichero reescrito) o si el fichero desapareció entre tick y clic → **"contenido no
disponible"**, un único estado de error.

Implementado en F5 (`views.read_content`, `GET /api/content?ids=1,2,3`):
- `ids`: solo enteros separados por comas, **máximo 200**; cualquier otra cosa → 400.
- Además del `uuid`, el bloque en el índice guardado debe ser del **tipo** guardado (un
  `tool_result` no puede devolver el texto de otro bloque si la línea cambió).
- La ruta leída debe estar **bajo la raíz** de `~/.claude/projects` (defensa en profundidad:
  la caché es nuestra, pero un fichero que no es de Claude Code nunca se lee).
- Recorte a **20.000 caracteres** por bloque (`truncated: true`): hay salidas de varios MB.
- La respuesta es JSON con `nosniff`; el cliente lo pinta con `textContent` (§9). Verificado en
  Chrome: un `tool_result` con `<script>alert(1)</script>` se ve como texto, 0 `alert`, 0
  elementos `<script>`.

### 6.6 Coste

- Calculado **al consultar** desde `requests` × `prices.toml`; no se almacena. Corregir un precio
  corrige todo el historial.
- Clave de precio: modelo tras **quitar solo el sufijo de fecha** (`claude-haiku-4-5-20251001`
  → `claude-haiku-4-5`). Nada más se fusiona: `claude-opus-5` y `claude-opus-5-5` son modelos
  distintos.
- Modelo sin precio → `?`, nunca 0. Vale para **toda suma** (ventana, hoy, sesión, herencia), con
  tres casos: todo con precio `$X`; parte `$X+` (y cuántas faltan); nada con precio `?`.
- **Supuestos** (no verificados, marcados en el código): `speed` ausente (1.712 líneas de
  versiones antiguas) = precio estándar; `inference_geo` ausente (solo las 18 `<synthetic>`) = ×1;
  el ×1,1 de `inference_geo = "us"` no se aplica a las búsquedas web (0 búsquedas en disco).
- Los precios de caché de **fast mode** se derivan aplicando los multiplicadores de caché de
  cada modelo a la tarifa fast oficial; están escritos explícitamente en `prices.toml`.
- **Modificadores** [verificado en la página oficial de precios, `findings.md` §5]:
  `speed = "fast"` usa la tabla de fast mode; `inference_geo = "us"` multiplica todo por 1,1;
  cada búsqueda web suma 0,01 $. Un valor desconocido en cualquiera de estos campos → `?`.
- La fórmula está **validada al microdólar**: aplicada a la sesión `36b96010` da 2,012743 $ y
  0,017852 $, idénticos a los `costUSD` de su `cost-state` [verificado].
- Se etiqueta siempre **"coste estimado"**, con tooltip: precios públicos actuales aplicados a
  sesiones pasadas; no es la factura.
- `prices.toml` lleva la fecha y la URL de la fuente (platform.claude.com, consultada el
  29-09-2026).

## 7. Interfaz

**traza es un navegador, no un dashboard de estado.** La referencia visual del autor (PRODUCT.md,
estilo fintech claro, elegido deliberadamente por el autor el 30-09-2026) pone el peso en
tarjetas de cifras; aquí las cifras son **contexto** y el peso está en el panel de trabajo
(árbol + juicio). Por eso la fila de tarjetas es baja y el panel de trabajo existe y ocupa su
sitio **desde F3**, aunque en F3 solo muestre el resumen de la sesión.

```
┌──┬ traza   [ Live | All ] ───────────────────────────────────────────────────────────────┐
│▣ │ ┌ $412.30 est. ─────┐ ┌ ● Live now ─────────────────────┐ ┌ 2 ────┐ ┌ 5 ─────┐       │
│  │ │ last 30 days      │ │ CV update · Explore · Bash 12s │ │active │ │agents  │  (bajas)│
│  │ └───────────────────┘ └ +2 more ─────────────────────────┘ └───────┘ └────────┘       │
│  │ ┌ Sessions ──────────────────────┐ ┌ Panel de trabajo ─────────────────────────────┐ │
│  │ │ CV update  ⚠2   ● tool   $1.92 │ │ F3: resumen de la sesión seleccionada         │ │
│  │ │  cv-project           2m ago   │ │ F4: árbol de agentes · F5: vista de juicio    │ │
│  │ │ joblens         ○ idle   $0.15 │ │ (≈60 % del ancho, alto completo)              │ │
│  │ │  +inherits 1,274 from 7bc0…    │ │                                               │ │
│  │ └────────────────────────────────┘ └───────────────────────────────────────────────┘ │
├──┴───────────────────────────────────────────────────────────────────────────────────────┤
│ parser health: 0 unknown · 54,915 ignored (14 types) · internal cost not broken down    │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

Decisiones de F3 (revisión del brief, 30-09-2026):

- **Jerarquía:** fila de tarjetas **baja** (una línea de cifra + etiqueta). Debajo, dos columnas a
  alto completo: tabla de sesiones (≈40 %) y **panel de trabajo** (≈60 %), reservado desde F3.
  En móvil/estrecho, una columna: tarjetas, sesiones, panel.
- **Estado y señal son dos cosas y van separadas:**
  - *Estado* (§7.1, a nivel de sesión = el de su agente más activo): `tool` (herramienta en
    vuelo), `thinking`, `idle`. Pastilla en colores **neutros** (azul, azul violáceo, gris). Nunca
    rojo ni coral.
  - *Señales* (§7.2): badges rojos pequeños junto al título, con el número; al pulsar llevan a
    los eventos. En F3 solo existen las que no requieren análisis: errores de herramienta
    (`is_error`) y `api_error`. Bucles y reintentos llegan en F6.
  - Así la tabla puede decir "idle, pero con 2 errores".
- **Sesión "viva"** = su estado (§7.1) es `tool` o `thinking`; **no** depende de "modificado hace
  < N min", porque las respuestas se escriben de golpe al terminar (`findings.md` §1) y un modelo
  que tarda 3 min en contestar no toca el fichero. Estado de una sesión = el del agente más
  activo (`tool` > `thinking` > `idle`); el de un agente sale de su **última línea**:
  - `tool_use` sin su `tool_result`, **de la última respuesta del agente y sin prompt
    posterior** → `tool`. Las herramientas de respuestas anteriores ya acabaron o se
    abandonaron (el modelo no vuelve a responder sin sus resultados): contarlas dejaría una
    sesión en `tool` para siempre por una herramienta interrumpida [encontrado en F3 con la
    fixture de la copia, test];
  - última línea = **texto del modelo** → `idle` (si quisiera seguir, su última línea sería un
    `tool_use`). **No se usa `stop_reason`**: las versiones 2.1.24–2.1.27 lo escriben `null`
    incluso en la respuesta final (42 ficheros acaban así) [verificado en F3];
  - `prompt` que el modelo **no contesta** → `idle`: salidas de comando local (`/model`,
    `/cost`: 0 de 21 contestadas) e interrupciones (1 de 9) [verificado en F3; el parser las
    marca con `origin` = `local-command` / `interrupted`];
  - cualquier otro `prompt` (humano 99 % contestados, `isMeta` 92 %, notificaciones 92 %),
    `tool_result`, `api_error`, `compact_boundary` → `thinking` (el modelo tiene el turno).
  Única constante: si el fichero **del agente** lleva **> 10 min** sin cambiar (el mismo umbral
  de herramienta colgada, §7.2), ese agente cuenta como `idle`; se aplica por agente para que
  un subagente interrumpido no deje la sesión en `tool` mientras el principal sigue trabajando.
  La pantalla se refresca también cada 30 s, porque estas transiciones solo dependen del tiempo
  y no generan ningún tick de ficheros.
- **Resumen de sesión en F4:** pasa a una **cabecera de una línea** encima del árbol (título,
  coste propio/heredado, modelos) con el detalle en un acordeón plegado. El panel de trabajo
  nunca apila resumen + árbol + juicio a la vez.
- **Tarjeta "Live now" (coral):** la sesión viva más reciente: título, agente activo, herramienta
  y cuánto lleva. Con **2+ vivas**: la más reciente + "+N more", que al pulsar activa el filtro
  `Live`. Con **0 vivas**: no se queda un hueco gris; pasa a tono neutro con "Last active:
  <título> · hace N min". Sin sparkline en F3 (necesita agregación y dibujo propio; se decide en
  F4 con datos por agente).
- **Tarjeta negra (coste de la ventana):** suma de lo que tiene precio. Si alguna petición no
  tiene precio (`?`), la cifra lleva un **"+"** y una línea "N requests unpriced, not included";
  nunca se presenta un parcial como total. Hoy: 0 peticiones sin precio en disco [verificado].
  Siempre con la etiqueta *estimated*.
- **Píldoras `Live | All`:** solo filtran la **tabla**; el resto de la pantalla no cambia. `All` =
  todo lo que hay en disco (ya es la ventana de 30 días: no se llama "Last 30 days" porque no es
  una selección). Orden: última actividad.
- **Barra de salud del parser:** franja fina a ancho completo, fija abajo, siempre visible; no es
  una tarjeta más.
- **Tarjeta "Cost today"** (revisión de F3): sustituye a "Live sessions", que repetía el "1" de la
  tarjeta coral. Coste estimado de las peticiones desde la medianoche **local**, cada una una vez;
  con "+" si alguna no tiene precio.
- **Barra lateral:** empieza con **un solo icono** (Sessions). Cada vista añade el suyo cuando
  existe (Agents en F4, Signals y Parser health en F6).
- **Tabla de sesiones:** título (+ proyecto en segunda línea, + badges de señal), estado, coste
  estimado, última actividad. **Sin columna de modelo**: 4 de 57 sesiones usan 2–3 modelos
  [verificado]; los modelos van al panel de trabajo con su número de peticiones.
- **Coste de una sesión con peticiones heredadas** (§6.2) — **decidido: opción (a).** Coste
  propio en grande y debajo, pequeño y gris, "inherits N requests from <sesión> (+$X)". Las
  sumas cuadran y el dato heredado no se esconde.
- **Clic en una sesión (F3):** la fila queda seleccionada y el panel de trabajo muestra su
  resumen: título, proyecto, inicio, última actividad, modelos (con nº de peticiones), coste
  propio y heredado, nº de agentes. F4 sustituye ese resumen por el árbol.
- **Tipografía:** Manrope (OFL), servida desde `static/` con su `OFL.txt`; ninguna petición a
  CDNs ni a Google Fonts (100 % local).
- **Idioma de la interfaz:** inglés.

- **Árbol:** colapsable; el estado de colapsado vive en memoria del JS y los ticks SSE no lo
  resetean. Coste: **propio en grande**; en nodos con hijos, **acumulado pequeño en gris**.
  Columna ordenable **coste por token de salida**.
- **Vista de juicio:** encargo arriba, timeline de herramientas en medio, resultado devuelto al
  padre abajo. Agrupada por turno, turnos cerrados plegados, últimos 50 turnos + "cargar
  anteriores" (sin virtual scrolling en v1). Implementada en F5 (`views.agent_view`,
  `GET /api/sessions/{s}/agents/{a}`):
  - Se abre pulsando el nombre de un agente en el árbol; sustituye al árbol (nunca se apilan).
  - **Turno** = una petición del modelo (sus bloques `text` y `tool_use`) con el `tool_result`
    de cada herramienta **emparejado** por `tool_use_id`. Cada turno lleva modelo, coste,
    tokens de salida y el aviso de plausibilidad (§8). Texto sin `requestId` (`<synthetic>`)
    es su propio turno. Prompts y eventos de sistema (`api_error`, compactación, notificación,
    resultado sin su llamada) van como elementos aparte, plegados.
  - **Encargo** = primer prompt del subagente (fuera de la timeline). El principal no tiene.
  - **Resultado devuelto**, solo si el agente está `done`: en primer plano, el `tool_result`
    que recibió el padre (exactamente lo que vio); en segundo plano o skill fork, su último
    texto ("Final answer"). Sin fin registrado → se dice, no se inventa.
  - Plegado: abierto por defecto solo el último turno; lo que el usuario abre o cierra se
    respeta en cada tick. El contenido se pide al desplegar y se guarda por id.
  - "Cargar anteriores" pide `before=<start>` y desde entonces cada tick pide
    `start_at=<lo más antiguo cargado>`: lo cargado no se pierde (la timeline solo crece por
    el final). Si nada cambió, no se redibuja (conserva scroll y foco).
### 7.1 Estados de agente

| Estado | Regla | Tipo |
|---|---|---|
| Herramienta en vuelo (`tool`) | `tool_use` de su última respuesta sin `tool_result` ni prompt posterior | exacto |
| Pensando (`thinking`) | última línea deja el turno al modelo (ver §7, reglas con datos); **también** texto cuyo `stop_reason` (último no nulo de su petición) es `tool_use`, o es null y su fichero se tocó hace < 30 s: la respuesta aún se está escribiendo (findings.md §F4) | heurístico |
| Esperando / inactivo (`idle`) | última línea es texto con `end_turn` (u otro fin), o null con el fichero quieto ≥ 30 s (≤ 2.1.268 escribían null al terminar); un prompt que no se contesta; o fichero quieto > 10 min | heurístico |
| Terminado (`done`, solo subagentes) | algún evento de la sesión lo nombra (`agent_ref`) con estado terminal: `toolUseResult.status` `completed` (primer plano) o `forked` (skill fork), o `<status>` `completed`/`failed`/`stopped` de una `task-notification` (segundo plano). **No** `async_launched`: es el resultado de lanzarlo en segundo plano (66 de 93 subagentes), no de terminar. Un resultado sin `toolUseResult` (dentro de un subagente) también vale. **Toda prueba de fin cuenta solo si es posterior a la última línea del agente**: una anterior es un lanzamiento; líneas nuevas tras un fin = agente reanudado, vuelve a trabajar. Manda sobre los demás estados | exacto |

Reglas detalladas y su evidencia: §7, "Sesión viva".

El agente principal nunca está "terminado": está "inactivo".

**Estas reglas dependen de la versión de Claude Code.** El formato del JSONL no está
documentado y ha cambiado dentro de la 2.1.x: `stop_reason` null al terminar (≤ 2.1.268), la
línea de texto de un subagente escrita antes que su herramienta (2.1.284), la notificación de
fin encolada como `attachment`. Cada regla cita la versión en la que se midió; hay que volver a
medirlas en disco cada pocos meses o al ver un estado raro en el panel.

### 7.2 Señales automáticas (deterministas, con test cada una)

Se muestran como **señal**, no veredicto; al pulsarlas llevan a los eventos que las causan.
Los umbrales viven en un solo bloque de constantes.

| Señal | Regla | Muestra alerta |
|---|---|---|
| Error de herramienta | `tool_result.is_error` **sin** `denial` (se ejecutó y falló) | sí |
| Bloqueo | `tool_result.is_error` **con** `denial` (hook, regla de permisos, usuario, auto mode): no se ejecutó; se cuenta aparte, no es un fallo | sí |
| `api_error` / compactación | subtipos de `system` | sí |
| Bucle | mismo agente, misma herramienta, mismo hash de JSON canónico (claves ordenadas, sin espacios) ≥ 3 veces, **sin error intermedio** | sí |
| Reintento que también falló | error → misma herramienta → error | sí |
| Reintento que funcionó | error → misma herramienta → éxito | no (informativa) |
| Herramienta colgada | `tool_use` sin resultado > 10 min, **excepto** si lanza un subagente que sigue escribiendo | sí |
| Coste por token de salida | columna ordenable | no |

## 8. Pruebas y evaluación (cómo se demuestra que los datos son correctos)

1. **Test de deduplicación por `requestId`** (exhaustivo): fixture con peticiones multilínea;
   los tokens deben contarse una vez.
2. **Test oráculo contra `cost-state`**: compara **tokens por modelo** (no dólares) calculados
   por traza con el `modelUsage` que escribe Claude Code, en la ventana **[`startTime` de
   `cost-state`, posición de la línea]**: `cost-state` mide el gasto del proceso desde que
   arrancó, no el de toda la sesión (`findings.md` §4). [verificado] Coincidencia **exacta** en
   2 de 3 sesiones con `cost-state` en este disco. Es escaso (4 líneas en todo el disco): es una
   prueba puntual de exactitud, no una suite de regresión. Las llamadas internas (Haiku) se
   excluyen de la comparación. El test compara también **dólares** (tolerancia 1 µ$) y solo lee el
fichero principal: las dos sesiones sanas no tienen carpeta `subagents/` [verificado]. [sin
verificar] si `cost-state` incluye el gasto de los subagentes; no hay aún un caso que lo pruebe. **Política:** el test recorre todas las sesiones con
   `cost-state`, informa cuáles cuadran y cuáles no, y exige que cuadren **al menos** las de una
   lista escrita de sesiones sanas (`36b96010`, `598796c2`) **que sigan en disco**. Las que ya no
existan (limpieza de 30 días) se informan como "ausente" y no ponen el test en rojo; si no
queda ninguna, el test se salta con ese motivo. Las copias (sesiones que comparten
   `requestId` con otra) se informan como *known issue*, no como fallo. Así el test es verde hoy
   y se pone rojo solo si una sesión sana deja de cuadrar. Este test corre solo en local: depende
   de datos reales que no están en el repo.
   **Límite estructural del oráculo:** solo detecta los errores que **no comparte** con el
   parser. Si los dos caminos interpretan el JSONL igual de mal, coinciden y el test pasa. Así
   vivió desde F1 hasta F4 el bug de `output_tokens` (se tomaba el de la primera línea; crece
   entre líneas): el oráculo y la caché aplicaban la misma regla y la fixture repetía el mismo
   valor en todas las líneas. Se encontró midiendo con datos reales (207.240 frente a 1.827
   tokens), no por un test. Por eso, además del oráculo:
   - **tests de regla con datos que distinguen las alternativas** (`test_critical.py` §3:
     valores 100/200/300 en todos los órdenes; "primera" y "última" fallan, solo "mayor" pasa);
   - **comprobación de plausibilidad**, un aviso contado y no una aserción: una petición que
     declara menos de 1 token de salida por cada 40 caracteres escritos (10 veces menos que una
     estimación generosa) se cuenta en la barra de salud y se marca con `?` en su agente. En
     disco: 373 de 8.786 (p. ej. 31 tokens para 10.316 caracteres de JSON). traza no puede
     corregir ese número: lo escribió así Claude Code.
3. **Tests del parser** por tipo de línea, incluidos `<synthetic>`, sin `requestId`, `unknown`.
4. **Tests del watcher:** línea parcial, truncado, borrado, idempotencia al reprocesar.
5. **Test de seguridad:** una salida de herramienta con `<script>` no se ejecuta; `Host` ajeno →
   rechazo.
6. **Fixtures:** pequeñas y **sanitizadas**. Los JSONL reales contienen datos personales y
   posibles secretos y **nunca** se commitean.

## 9. Seguridad

- Escucha **solo en `127.0.0.1`**.
- Valida la cabecera `Host` (contra DNS rebinding); `Origin`, si viene, debe coincidir; si no
  viene, se acepta. Sin token en v1 (panel de solo lectura).
- **Riesgo aceptado (revisión de seguridad de F3):** el puerto de loopback lo puede abrir
  **cualquier cuenta local del equipo**, que así vería títulos, proyectos y costes que en disco
  solo puede leer el autor (`~/.claude` es de su usuario). En un portátil personal de un solo
  usuario no cruza ninguna frontera; en una máquina compartida sí. Mitigación preparada si hace
  falta: token aleatorio por arranque en la URL que abre `traza serve` (cookie `HttpOnly` +
  `SameSite=Strict` tras el primer acceso). Decisión del autor, no aplicada en v1.
- Revisado en F3 y cubierto por test: DNS rebinding (Host ajeno → 400), lectura cross-origin
  (sin CORS; `Origin` ajeno o `null` → 403, también en SSE), JSON leído como script (`nosniff`),
  XSS (solo `textContent` + CSP `script-src 'self'` sin inline), sin CSRF (todo es GET de solo
  lectura), sin traversal (el id de sesión solo llega a SQLite como parámetro).
- **Todo** el texto de los JSONL se pinta con `textContent`, nunca `innerHTML` (XSS).
- Solo lectura sobre `~/.claude/`: traza nunca escribe ahí.
- Estado propio en `~/.traza/` (config + `traza.db`), separado de los datos de Claude Code.

## 10. SSE

- Cada evento lleva `id:` (el `events.id`) y la `cache_generation`; el cliente se conecta a
  `/events?gen=…`. Si la generación no coincide (la BD se reconstruyó), el cliente recarga en vez
  de reanudar desde `Last-Event-ID`.
- Latido cada 15 s (evento `ping`, ver abajo).
- Cola por cliente con tamaño máximo; si se llena, se descarta lo más viejo; nunca se bloquea
  al watcher.

- **Latido con nombre (`event: ping`)**, no un comentario: `EventSource` no expone los comentarios
  al JS, así que el cliente no podría detectar una conexión muerta (portátil suspendido, proxy
  que corta sin cerrar). Cada 15 s.
- **Indicador de conexión, tres estados** [probado en Chrome parando y arrancando el servidor]:

  | Estado | Cuándo | Aspecto |
  |---|---|---|
  | `open` "Watching" | llegó `hello`, `tick` o `ping` hace < 40 s | punto verde |
  | `reconnecting` "Reconnecting…" | error de red, o 40 s sin ningún evento; el navegador reintenta | punto ámbar |
  | `lost` "Disconnected · data may be stale" | > 10 s sin conexión; sigue reintentando, sin volver a ámbar | punto rojo |

  Medido: servidor parado → ámbar en ~3 s → rojo 10 s después, sin parpadeo; servidor de vuelta →
  verde en ~2,5 s y sin recargar la página (misma generación). Tras reconectar se refrescan los
  datos (`hello` → refresco), porque pudo cambiar algo mientras tanto.

## 11. Riesgos

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Claude Code cambia el formato JSONL | panel roto o números falsos | parser tolerante, `unknown` visible, tests, `parser_version` |
| Discrepancia del oráculo en `3416476c` (`cost-state` habla de `claude-opus-5-5`, el JSONL solo tiene `claude-opus-5` y otras cifras) | el oráculo no es fiable en sesiones copiadas | [verificado] `3416476c` es copia de `7bc000bb`. Las sesiones copia se excluyen del test oráculo (§8) |
| Precios desactualizados | coste estimado erróneo | `prices.toml` editable, `?` para modelos desconocidos, etiqueta "estimado" |
| Llamadas internas no visibles en JSONL | coste inferior al real | mostrar "coste interno no desglosado" |
| Rendimiento de `stat()` en Windows con antivirus | CPU / latencia | medir; subir intervalo si hace falta |
| Datos personales en fixtures o capturas del GIF | fuga de información | fixtures sintéticas; revisar el GIF antes de publicar |

## 12. Roadmap de alcance

**v1 (MVP):** todo lo de §4.1 a §10.

**v1.x (solo si se echa en falta):** hooks HTTP para "necesita input" y estado exacto.

**v2:**
- Juez LLM **local y gratuito** (p. ej. vía Ollama) que puntúa el resultado de cada subagente
  frente a su encargo, **calibrado** con un conjunto de casos etiquetados por el autor para medir
  cuánto acierta. Depende del hardware (GPU/VRAM), a comprobar entonces.
- Mostrar la salida de los hooks (`attachment`) en la timeline.

**Ideas para más adelante:** gráficas de gasto por día/semana, alertas de presupuesto,
exportar una sesión como informe, replay animado.
