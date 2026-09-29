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
- Dentro de `3416476c` hay **4 `uuid` repetidos con contenido distinto** (todos `attachment`).
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
  coste de céntimos). Aparecerá en traza como una sesión más. Borrarla es decisión del autor;
  Claude Code la eliminará a los 30 días.
