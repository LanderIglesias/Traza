// traza — pantalla de sesiones (F3). Todo texto que viene de los JSONL se pinta con
// textContent: nunca se interpreta como HTML (design.md §9).

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";   // identificador del estándar, no una petición
const STATE_LABEL = { tool: "Running tool", thinking: "Thinking", idle: "Idle", done: "Done" };
// collapsed: "sesión/agente" plegados; sobrevive a los re-render de cada tick SSE (F4)
const ui = { data: null, filter: "all", selected: null, collapsed: new Set(), sort: null, tab: "tree" };
const ORPHAN_MARK = { sin_tool_use_id: "skill fork", padre_no_encontrado: "parent unknown" };

// --- formato ---------------------------------------------------------------------------------
// Tres casos (§6.6): todo con precio "$X", parte "$X+", nada con precio "?" (v === null)
function money(v, unpriced = 0) {
  if (v === null) return "?";
  const plus = unpriced ? "+" : "";
  if (v === 0 && !unpriced) return "$0.00";
  if (v > 0 && v < 0.01) return `<$0.01${plus}`;
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${plus}`;
}
function ago(iso) {
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function since(iso) {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}
const shortId = (id) => id.slice(0, 8);
// mcp__<servidor>__<herramienta>: en espacios cortos basta la herramienta (el nombre entero va en title)
const toolLabel = (t) => (t?.startsWith("mcp__") ? t.split("__").at(-1) : t);
const titleOf = (s) => s.title || `Untitled session · ${shortId(s.id)}`;

// --- construcción de nodos sin HTML ------------------------------------------------------------
function el(tag, props = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") n.textContent = v;
    else if (k.startsWith("data-") || k.startsWith("aria-")) n.setAttribute(k, v);
    else n[k] = v;
  }
  n.append(...children.filter(Boolean));
  return n;
}
function icon(paths) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const d of paths) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}
const CHEVRON = ["M6 9l6 6 6-6"];
// Señales (§7.2): una pista para mirar, no un veredicto. En rojo las alertas; en gris, las que informan.
const SIGNAL_LABEL = { error: "failed tool call", api_error: "API error", loop: "loop",
  retry_failed: "failed retry", hung: "no result for 10+ min", blocked: "blocked",
  compaction: "context compacted", retry_ok: "retried ok",
  unfinished: "unfinished: the session ended with this tool still running" };
const ALERT_KINDS = ["error", "api_error", "loop", "retry_failed", "hung"];
const plural = (n, w) => (n === 1 || w.endsWith("min") ? `${n} ${w}`
  : w.endsWith("y") ? `${n} ${w.slice(0, -1)}ies` : `${n} ${w}s`);
const alertText = (counts) => ALERT_KINDS.filter((k) => counts?.[k])
  .map((k) => plural(counts[k], SIGNAL_LABEL[k])).join(" · ");
const ALERT = ["M12 8v5", "M12 16.5v.01", "M10.3 3.9 2.6 17.3A2 2 0 0 0 4.3 20h15.4a2 2 0 0 0 1.7-2.7L13.7 3.9a2 2 0 0 0-3.4 0Z"];

function pill(state) {
  return el("span", { className: "pill", "data-state": state, text: STATE_LABEL[state] || state });
}

// --- render ----------------------------------------------------------------------------------
function renderCards(d) {
  $("window-cost").textContent = money(d.window.cost, d.window.unpriced);
  const note = $("window-note");
  note.hidden = !d.window.unpriced;
  note.textContent = d.window.unpriced
    ? `${d.window.unpriced.toLocaleString("en-US")} requests unpriced, not included` : "";
  $("today-cost").textContent = money(d.today.cost, d.today.unpriced);
  $("active-agents").textContent = d.counts.active_agents;

  const live = d.sessions.filter((s) => s.live);
  const card = $("live-card");
  card.dataset.live = String(live.length > 0);
  const more = $("live-more");
  if (live.length) {
    const s = live[0];                                    // la viva más reciente
    $("live-status").textContent = "Live now";
    $("live-title").textContent = titleOf(s);
    $("live-detail").textContent = s.activity
      ? `${s.activity.agent} · ${toolLabel(s.activity.tool)} · ${since(s.activity.since)}`
      : `${STATE_LABEL[s.state]} · ${s.project}`;
    more.hidden = live.length < 2;
    more.textContent = `+${live.length - 1} more`;
  } else {
    const last = d.sessions[0];
    $("live-status").textContent = "Nothing running";
    $("live-title").textContent = last ? `Last active: ${titleOf(last)}` : "No sessions on disk yet";
    $("live-detail").textContent = last ? `${ago(last.last_activity)} · ${last.project}` : "";
    more.hidden = true;
  }

  const h = d.health;
  const health = $("health");
  health.dataset.warn = String(h.unknown > 0);
  $("health-text").textContent =
    `Parser health · ${h.unknown.toLocaleString("en-US")} unknown lines · ` +
    `${h.ignored.toLocaleString("en-US")} lines ignored on purpose (${h.ignored_types} types) · ` +
    (h.implausible ? `${h.implausible.toLocaleString("en-US")} requests with implausibly low output tokens · ` : "") +
    `$ = value at public API prices (what paying per token would cost; not what a subscription pays)` +
    // coste interno (llamadas de Claude Code que no se escriben): solo si hay algo medido
    (h.internal_cost ? ` · internal cost measured: at least ${money(h.internal_cost.cost)} in ${plural(h.internal_cost.sessions, "session")}` : "");
  $("health-internal").hidden = !h.internal_cost;
  if (h.internal_cost) {
    const ic = h.internal_cost;
    $("health-internal-text").textContent =
      `Claude Code also calls models for its own work (session titles, summaries) and does not write those calls to the session files. ` +
      `Its own cost records measure them in ${plural(ic.sessions, "session")}: at least ${money(ic.cost)}, ` +
      `${(ic.share * 100).toFixed(2)} % of the ${money(ic.measured)} those records cover. It is a floor: internal calls to a model ` +
      `the session also uses cannot be told apart. Sessions without a cost record cannot be measured.`;
  }
  $("health-ignored").replaceChildren(...Object.entries(h.ignored_by_type).map(([t, n]) =>
    el("li", {}, el("span", { text: t }), el("span", { text: n.toLocaleString("en-US") }))));
}

function renderRows(d) {
  // si el foco estaba en una fila, vuelve a la misma fila tras redibujar (cada tick SSE redibuja)
  const focusedId = document.activeElement?.closest?.("#rows") ? document.activeElement.dataset.id : null;
  const rows = d.sessions.filter((s) => ui.filter === "all" || s.live);
  $("sessions-count").textContent = `${rows.length} of ${d.sessions.length}`;
  const empty = $("rows-empty");
  empty.hidden = rows.length > 0;
  empty.textContent = ui.filter === "live" ? "No session is working right now." : "No sessions on disk yet.";
  const list = rows.map((s) => {
    const btn = el("button", { type: "button", className: "row", "data-id": s.id, "aria-current": String(s.id === ui.selected) },
      el("span", { className: "row-title" },
        el("span", { className: "t", text: titleOf(s) }),
        s.alerts ? el("span", { className: "badge", title: alertText(s.signals) },
          icon(ALERT), el("span", { text: String(s.alerts) })) : null,
        // la herramienta no llegó a ejecutarse (hook, regla de permisos, usuario): no es un fallo
        s.blocked ? el("span", { className: "badge badge-muted", title: `${s.blocked} tool calls blocked before running (hooks, permission rules, user)`,
          text: `${s.blocked} blocked` }) : null),
      el("span", { className: "row-sub", text: `${s.project} · ${shortId(s.id)}` }),
      el("span", { className: "row-right" },
        el("span", { className: "row-cost", text: money(s.cost, s.unpriced) }),
        el("span", { className: "row-meta" }, pill(s.state), el("span", { text: ago(s.last_activity) }))),
      ...s.inherits.map((i) => el("span", {
        className: "inherit-line",
        text: `inherits ${i.requests.toLocaleString("en-US")} requests from ${shortId(i.from)} (+${money(i.cost, i.unpriced)})`,
      })));
    btn.addEventListener("click", () => select(s.id));
    return el("li", {}, btn);
  });
  $("rows").replaceChildren(...list);
  if (focusedId) $("rows").querySelector(`[data-id="${CSS.escape(focusedId)}"]`)?.focus();
}

// Respuestas que llegan desordenadas (clic + tick + refresco de 30 s): solo pinta la última pedida.
let panelSeq = 0;
let refreshSeq = 0;

async function renderPanel() {
  const seq = ++panelSeq;
  const body = $("panel-body");
  if (!ui.selected) {
    body.hidden = true;
    $("panel-empty").hidden = false;
    return;
  }
  const wanted = ui.selected;
  const r = await api(`/api/sessions/${encodeURIComponent(wanted)}`);
  if (seq !== panelSeq) return;   // llegó una petición más nueva: no pintar datos viejos
  if (r.status === 404) {          // la sesión desapareció del disco (limpieza de 30 días)
    ui.selected = null;
    return renderPanel();
  }
  if (!r.ok) return;               // fallo pasajero: se conserva la selección; el próximo tick reintenta
  const s = await r.json();
  if (seq !== panelSeq) return;
  $("panel-empty").hidden = true;
  body.hidden = false;
  $("p-title").textContent = titleOf(s);
  $("p-sub").textContent = `${s.project} · ${s.id}`;
  $("p-state").replaceWith(Object.assign(pill(s.state), { id: "p-state" }));
  $("p-cost").textContent = money(s.cost, s.unpriced);
  const inh = $("p-inherit");
  inh.hidden = !s.inherits.length;
  inh.textContent = s.inherits.map((i) =>
    `Also contains ${i.requests.toLocaleString("en-US")} requests owned by ${shortId(i.from)} (+${money(i.cost, i.unpriced)}), not counted here`).join(" · ");
  $("p-started").textContent = s.started ? new Date(s.started).toLocaleString() : "?";
  $("p-last").textContent = ago(s.last_activity);
  $("p-requests").textContent = s.requests.toLocaleString("en-US");
  $("p-agents").textContent = String(s.agents);
  $("p-models").replaceChildren(...Object.entries(s.models).map(([m, n]) =>
    el("li", {}, el("span", { text: m }), el("span", { className: "n", text: `${n.toLocaleString("en-US")} ${n === 1 ? "request" : "requests"}` }))));
  const judging = ui.judge?.sid === s.id;
  $("p-details").hidden = judging;
  document.querySelector(".tabs").hidden = judging;
  $("tree-wrap").hidden = judging || ui.tab !== "tree";
  $("trace").hidden = judging || ui.tab !== "trace";
  $("judge").hidden = !judging;
  if (judging) renderJudge();
  else if (ui.tab === "trace") await renderTrace(s);
  else renderTree(s);
}

// --- árbol de agentes (F4) -----------------------------------------------------------------------
const perOut = (n) => (n.cost === null || !n.output ? null : (n.cost / n.output) * 1e6);
const SORT_KEY = { cost: (n) => n.cost, total: (n) => n.total.cost, per_out: perOut };

function sortSiblings(list) {
  if (!ui.sort) return list;                       // sin orden elegido: por hora de inicio
  const key = SORT_KEY[ui.sort.key];
  return [...list].sort((a, b) => {
    const x = key(a), y = key(b);
    if (x === null || y === null) return (x === null) - (y === null);   // "?" siempre al final
    return (x - y) * ui.sort.dir;
  });
}

function agentRow(s, n, depth, hasKids) {
  const key = `${s.id}/${n.id}`;
  const open = !ui.collapsed.has(key);
  const name = n.id === "main" ? "Main agent" : n.type || `Agent ${n.id.slice(0, 7)}`;
  const toggle = hasKids
    ? el("button", { type: "button", className: "toggle", "aria-expanded": String(open),
        "aria-label": `${open ? "Collapse" : "Expand"} ${name}` }, icon(CHEVRON))
    : el("span", { className: "toggle-gap" });
  if (hasKids) toggle.addEventListener("click", () => {
    ui.collapsed[open ? "add" : "delete"](key);
    renderTree(s);
  });
  const desc = n.orphan === "sin_tool_use_id" ? n.task : n.description;
  const cell = el("div", { className: "agent-cell" }, toggle,
    el("span", {},
      n.orphan ? el("span", { className: "mark", "data-kind": n.orphan, text: ORPHAN_MARK[n.orphan] }) : null,
      el("button", { type: "button", className: "agent-name agent-open", text: name,
        "aria-label": `Open the timeline of ${name}` }),
      n.alerts ? el("button", { type: "button", className: "badge badge-btn node-alerts",
        title: alertText(n.signals), "aria-label": `${alertText(n.signals)}: show the latest` },
        icon(ALERT), el("span", { text: String(n.alerts) })) : null,
      desc ? el("span", { className: "agent-desc", text: desc, title: desc }) : null));
  cell.style.setProperty("--depth", depth);
  cell.querySelector(".agent-open").addEventListener("click", () => openJudge(s.id, n.id));
  cell.querySelector(".node-alerts")?.addEventListener("click", () => openJudge(s.id, n.id, n.alert_event));
  const po = perOut(n);
  const out = n.output === null ? "?" : n.output.toLocaleString("en-US") + (n.implausible ? " ?" : "");
  const statePill = pill(n.state);
  // subagente parado sin prueba de fin: Claude Code no escribió cuándo terminó (findings §F4)
  if (n.id !== "main" && n.state === "idle") statePill.title = "Idle · no end recorded on disk for this subagent";
  return el("tr", { "data-agent": n.id, "data-parent": n.parent ?? (n.orphan ? "(orphan)" : "(root)") },
    el("td", {}, cell),
    el("td", {}, statePill),
    // data-label: en móvil cada fila es una tarjeta y la cabecera no se ve
    el("td", { className: "num", "data-label": "Own", text: money(n.cost, n.unpriced) }),
    el("td", { className: "num", "data-label": "Incl. subagents", text: money(n.total.cost, n.total.unpriced) }),
    el("td", { className: "num", "data-label": "Output", text: out,
      // "?" solo si la suma del agente es implausible (design.md §8); las sueltas, en el título
      title: n.implausible
        ? `Its output tokens add up to far less than the text it wrote (${n.implausible_requests} requests with the count written wrong by Claude Code): the real output, and cost, are higher`
        : n.implausible_requests
          ? `${n.implausible_requests} of its requests report too few output tokens; the agent's total is still plausible`
          : "" }),
    el("td", { className: "num", "data-label": "$/1M out", text: po === null ? "–" : money(po) }));
}

function renderTree(s) {
  // cada tick redibuja las filas: el botón de plegar que tenía el foco lo recupera (§ accesibilidad)
  const focused = document.activeElement?.closest?.(".tree tr")?.dataset.agent;
  const kids = new Map();
  for (const n of s.tree) {
    const p = n.orphan ? "(orphan)" : n.parent ?? "(root)";
    if (!kids.has(p)) kids.set(p, []);
    kids.get(p).push(n);
  }
  const walk = (parent, depth, out) => {
    for (const n of sortSiblings(kids.get(parent) || [])) {
      const has = kids.has(n.id);
      out.push(agentRow(s, n, depth, has));
      if (has && !ui.collapsed.has(`${s.id}/${n.id}`)) walk(n.id, depth + 1, out);
    }
    return out;
  };
  $("tree-rows").replaceChildren(...walk("(root)", 0, []));
  const orphans = walk("(orphan)", 0, []);
  $("orphan-rows").replaceChildren(...(orphans.length ? [
    el("tr", { className: "orphan-h" }, el("td", { colSpan: 6, text: "Not attached to a parent agent" })),
    ...orphans] : []));
  if (focused) document.querySelector(`.tree tr[data-agent="${CSS.escape(focused)}"] .toggle`)?.focus();
  for (const th of document.querySelectorAll(".tree th[data-sort]")) {
    if (ui.sort?.key === th.dataset.sort) th.setAttribute("aria-sort", ui.sort.dir < 0 ? "descending" : "ascending");
    else th.removeAttribute("aria-sort");
  }
}

for (const th of document.querySelectorAll(".tree th[data-sort]")) {
  th.querySelector("button").addEventListener("click", () => {
    const k = th.dataset.sort;       // 1er clic: mayor primero; 2º: menor; 3º: sin orden
    ui.sort = ui.sort?.key !== k ? { key: k, dir: -1 } : ui.sort.dir < 0 ? { key: k, dir: 1 } : null;
    renderPanel();
  });
}

function select(id) {
  ui.selected = id;
  ui.judge = null;
  renderRows(ui.data);
  renderPanel();
}

function setFilter(f) {
  ui.filter = f;
  for (const b of document.querySelectorAll("[data-filter]")) b.setAttribute("aria-pressed", String(b.dataset.filter === f));
  renderRows(ui.data);
}

// --- datos en vivo -----------------------------------------------------------------------------
let pending = null;
async function refresh() {
  const seq = ++refreshSeq;
  const r = await api("/api/overview");
  const data = await r.json();
  if (live.run && data.run !== live.run) return stopped();   // otro servidor en el puerto
  live.run = data.run;
  if (seq !== refreshSeq) return ui.data;   // una más nueva ya pintó (o pintará)
  ui.data = data;
  renderCards(ui.data);
  renderRows(ui.data);
  if (ui.selected) renderPanel();
  return ui.data;
}
function refreshSoon() {                     // varios ticks seguidos → una sola petición
  clearTimeout(pending);
  pending = setTimeout(refresh, 150);
}

// Conexión en vivo (design.md §10): tres estados visibles.
//   open          "Watching"      llegó hello/tick/ping hace < SILENT_MS
//   reconnecting  "Reconnecting…" error de red o SILENT_MS sin nada; el navegador reintenta
//   lost          "Disconnected"  > LOST_AFTER_MS sin conexión; sigue reintentando
const SILENT_MS = 40000;        // el servidor late cada 15 s: 40 s sin nada = conexión muerta
const LOST_AFTER_MS = 10000;
const CONN_TEXT = { open: "Watching", reconnecting: "Reconnecting…", lost: "Disconnected · data may be stale",
                    stopped: "traza stopped · reload the page" };
const live = { es: null, gen: null, run: null, stopped: false, lastSeen: 0, downSince: 0 };

// Cada arranque de traza tiene su `run`. Si al reconectar responde otro (traza reiniciado u
// otro programa en el puerto), la pestaña se para en vez de pintar sus datos (auditoría F7).
function stopped() {
  live.stopped = true;
  live.es?.close();
  setConn("stopped");
  return ui.data;
}
const api = (url) => live.stopped ? Promise.reject(new Error("traza stopped")) : fetch(url);

function setConn(state) {
  $("conn").dataset.state = state;
  $("conn-text").textContent = CONN_TEXT[state];
}
function connect(gen) {
  live.gen = gen;
  live.es?.close();
  const es = (live.es = new EventSource(`/events?gen=${encodeURIComponent(gen)}`));
  const alive = () => { live.lastSeen = Date.now(); live.downSince = 0; setConn("open"); };
  es.addEventListener("hello", (e) => {
    if (JSON.parse(e.data).run !== live.run) return stopped();
    alive(); refreshSoon();                                          // tras reconectar: pudo cambiar algo
  });
  es.addEventListener("tick", () => { alive(); refreshSoon(); });
  es.addEventListener("ping", alive);
  es.addEventListener("reload", stopped);      // otra generación = otro arranque: no obedecer
  es.onerror = () => {
    if (live.stopped) return;
    live.downSince ||= Date.now();
    // cada reintento fallido vuelve a llamar aquí: no bajar de "lost" a "reconnecting" (parpadeo)
    if (Date.now() - live.downSince <= LOST_AFTER_MS) setConn("reconnecting");
    if (es.readyState === EventSource.CLOSED) setTimeout(() => connect(live.gen), 2000);
  };
}
setInterval(() => {                                    // vigilante
  if (!live.es || live.stopped) return;
  const now = Date.now();
  if ($("conn").dataset.state === "open" && now - live.lastSeen > SILENT_MS) {
    live.downSince = now;                              // conexión zombi (p. ej. tras suspender)
    setConn("reconnecting");
    connect(live.gen);
  } else if (live.downSince && now - live.downSince > LOST_AFTER_MS) {
    setConn("lost");
  }
}, 1000);

for (const b of document.querySelectorAll("[data-filter]")) b.addEventListener("click", () => setFilter(b.dataset.filter));
$("live-more").addEventListener("click", () => setFilter("live"));
setInterval(() => ui.data && renderCards(ui.data), 1000);   // relojes "hace N min" y "12s"
// Lo que cambia solo con el tiempo (una sesión que deja de estar viva a los 10 min, los "hace
// N min" de la lista) no genera ningún tick de ficheros: se refresca cada 30 s.
setInterval(refreshSoon, 30000);

(async function start() {
  try {
    const d = await refresh();
    connect(d.generation);
  } catch {                                   // servidor aún arrancando o caído: reintentar
    setConn("lost");
    setTimeout(start, 2000);
  }
})();

// --- vista de juicio (F5, design.md §7) -----------------------------------------------------------
// El texto nunca viene con la timeline: se pide por ids a /api/content al desplegar (§6.5) y se
// guarda aquí; dentro de una generación de caché, el contenido de un id no cambia.
const contentCache = new Map();
const SYSTEM_LABEL = { api_error: "API error", compact_boundary: "Context compacted",
  task_notification: "Subagent finished (notification)", unknown: "Unrecognised line",
  tool_result: "Tool result without its call" };
const PROMPT_LABEL = { human: "Prompt", "task-notification": "Notification", meta: "Injected by Claude Code",
  peer: "Message from an agent", interrupted: "Interrupted", "local-command": "Local command" };
let judgeSeq = 0;

function openJudge(sid, aid, focusEvent = null) {
  ui.judge = { sid, aid, from: null, sig: "", open: new Map(), focus: focusEvent };
  // nada del agente anterior mientras llega el nuevo
  for (const id of ["j-title", "j-sub", "j-cost", "j-count"]) $(id).textContent = "";
  setContentId($("j-task"), null);
  setContentId($("j-result"), null);
  $("j-items").replaceChildren();
  renderPanel();
  $("j-back").focus();
}
$("j-back").addEventListener("click", () => {
  const aid = ui.judge?.aid;
  ui.judge = null;
  // de vuelta a donde se entró: una barra de la Timeline o la fila del árbol
  renderPanel().then(() => document.querySelector(ui.tab === "trace"
    ? `.trace-bar[data-agent="${CSS.escape(aid)}"]`
    : `.tree tr[data-agent="${CSS.escape(aid)}"] .agent-open`)?.focus());
});
$("j-earlier").addEventListener("click", async () => {
  const j = ui.judge;
  const r = await api(`/api/sessions/${encodeURIComponent(j.sid)}/agents/${encodeURIComponent(j.aid)}?before=${j.data.start}`);
  if (!r.ok || ui.judge !== j) return;
  j.from = (await r.json()).start;        // desde ahí, cada refresco trae hasta el final
  j.sig = "";
  renderJudge();
});

async function renderJudge() {
  const j = ui.judge, seq = ++judgeSeq;
  const q = j.from !== null ? `?start_at=${j.from}` : "";
  const r = await api(`/api/sessions/${encodeURIComponent(j.sid)}/agents/${encodeURIComponent(j.aid)}${q}`);
  if (seq !== judgeSeq || ui.judge !== j) return;
  if (r.status === 404) { ui.judge = null; return renderPanel(); }
  if (!r.ok) return;
  const v = await r.json();
  if (seq !== judgeSeq || ui.judge !== j) return;
  const sig = JSON.stringify(v);
  if (sig === j.sig) return;              // nada cambió: no redibujar (conserva scroll y foco)
  j.sig = sig;
  j.data = v;
  paintJudge(j, v);
}

const hhmmss = (ts) => (ts ? new Date(ts).toLocaleTimeString() : "");
const agentName = (a) => (a.id === "main" ? "Main agent" : a.type || `Agent ${a.id.slice(0, 7)}`);

function paintJudge(j, v) {
  const a = v.agent;
  $("j-title").textContent = agentName(a);
  // el encargo ya se ve entero debajo: el subtítulo no lo repite
  $("j-sub").textContent = a.description || (a.orphan ? ORPHAN_MARK[a.orphan] : "");
  $("j-state").replaceWith(Object.assign(pill(a.state), { id: "j-state" }));
  $("j-cost").textContent = money(a.cost, a.unpriced);
  renderSummary(v.summary);

  $("j-task-block").hidden = v.task === null;
  if (v.task !== null && $("j-task").dataset.content !== String(v.task)) setContentId($("j-task"), v.task);

  const res = $("j-result");
  $("j-result-block").hidden = a.id === "main";
  if (v.result) {
    $("j-result-h").textContent = v.result.source === "parent_result" ? "Returned to parent" : "Final answer";
    if (res.dataset.content !== String(v.result.id)) setContentId(res, v.result.id);
  } else {
    $("j-result-h").textContent = "Returned to parent";
    setContentId(res, null);
    // 3. tres casos: terminó sin texto, sin fin registrado, o sigue trabajando
    res.textContent = a.state === "done" ? "The agent finished without writing any text."
      : a.state === "idle" ? "Nothing returned: no end is recorded on disk for this agent."
      : "Nothing yet: the agent is still working.";
  }

  const turns = v.items.filter((i) => i.kind === "turn").length;
  $("j-count").textContent = v.start > 0
    ? `· ${turns} turns shown, earlier ones hidden` : `· ${turns} turns`;
  $("j-earlier").hidden = v.start === 0;

  const lastTurn = v.items.findLastIndex((i) => i.kind === "turn");
  const focusedKey = document.activeElement?.closest?.("#j-items details")?.dataset.key;
  $("j-items").replaceChildren(...v.items.map((it, i) => itemNode(j, it, i === lastTurn)));
  if (focusedKey) $("j-items").querySelector(`details[data-key="${CSS.escape(focusedKey)}"] > summary`)?.focus();
  if (j.target) $("j-items").querySelector(`details[data-key="${CSS.escape(j.target)}"]`)?.parentElement.classList.add("is-target");
  if (j.focus !== null && j.focus !== undefined) goToEvent(j, v);
  fillContent($("judge"));
}

// Llevar a la alerta pulsada en el árbol: su turno abierto, centrado y con el foco.
function goToEvent(j, v) {
  const li = $("j-items").querySelector(`li[data-ev~="${j.focus}"]`);
  if (!li) {
    if (v.start > 0) { j.from = 0; j.sig = ""; renderJudge(); }   // está en turnos anteriores
    else j.focus = null;
    return;
  }
  j.focus = null;
  const d = li.querySelector("details");
  j.open.set(d.dataset.key, true);
  j.target = d.dataset.key;              // el resaltado sobrevive a los ticks (como el plegado)
  d.open = true;
  li.classList.add("is-target");
  li.scrollIntoView({ block: "center" });
  d.querySelector("summary").focus({ preventScroll: true });
}

function signalChips(sigs) {
  // error y bloqueo ya tienen su badge propio en el turno
  return (sigs || []).filter((x) => x.kind !== "error" && x.kind !== "blocked")
    .map((x) => el("span", { className: "sig-chip", "data-alert": String(x.alert), text: SIGNAL_LABEL[x.kind] || x.kind }));
}

function details(j, key, openByDefault, summary, body) {
  const head = el("summary", {}, ...summary);
  const d = el("details", { "data-key": key }, head, body);
  d.open = j.open.get(key) ?? openByDefault;   // lo que el usuario abrió o cerró se respeta
  // Solo el usuario fija la preferencia (clic, Enter o Espacio sobre <summary>): "toggle" salta
  // también cuando el código abre el último turno, y eso no es una elección suya.
  head.addEventListener("click", () => j.open.set(key, !d.open));
  d.addEventListener("toggle", () => { if (d.open && j.open.get(key)) fillContent(d); });
  return d;
}

function setContentId(node, id) {
  node.textContent = "";                 // "Loading…" hasta que llegue el nuevo
  delete node.dataset.state;
  if (id === null) delete node.dataset.content;
  else node.dataset.content = id;
}

function pre(id) {
  return el("pre", { className: "j-pre", "data-content": id });
}

function itemNode(j, it, isLast) {
  if (it.kind !== "turn") {
    const e = it.event;
    const label = it.kind === "prompt" ? PROMPT_LABEL[e.origin] || "Prompt" : SYSTEM_LABEL[e.kind] || e.kind;
    return el("li", { className: `t-item t-${it.kind}`, "data-ev": String(e.id) },
      details(j, `e${e.id}`, false,
        [el("span", { className: "t-kind", text: label }), el("time", { text: hhmmss(e.ts) }), ...signalChips(it.signals)],
        pre(e.id)));
  }
  const tools = new Map();
  let errors = 0, blocked = 0;
  for (const e of it.events) {
    if (e.kind === "tool_use") tools.set(e.tool_name, (tools.get(e.tool_name) || 0) + 1);
    if (e.result?.is_error) e.result.denial ? blocked++ : errors++;
  }
  const toolText = [...tools].map(([t, n]) => (n > 1 ? `${toolLabel(t)} ×${n}` : toolLabel(t))).join(" · ");
  const toolTitle = [...tools.keys()].join(" · ");
  const out = it.output === null ? "? out" : `${it.output.toLocaleString("en-US")} out${it.implausible ? " ?" : ""}`;
  const summary = [
    el("span", { className: "t-kind", text: toolText ? "Turn" : "Reply" }),
    el("time", { text: hhmmss(it.ts) }),
    toolText ? el("span", { className: "t-tools", text: toolText, title: toolTitle }) : null,
    errors ? el("span", { className: "badge", title: `${errors} tool results that failed` }, icon(ALERT), el("span", { text: String(errors) })) : null,
    blocked ? el("span", { className: "badge badge-muted", text: `${blocked} blocked` }) : null,
    ...signalChips(it.signals),
    el("span", { className: "t-cost", text: `${money(it.cost)} · ${out}`,
      title: it.implausible ? "Claude Code wrote an output token count far below what this turn wrote: the real output, and cost, are higher" : "Value at API prices and output tokens of this turn" }),
  ];
  const body = el("div", { className: "t-body" }, ...it.events.map((e) => {
    if (e.kind === "text") return el("div", { className: "t-text" }, pre(e.id));
    const r = e.result;
    const resLabel = !r ? "No result yet" : r.denial ? `Blocked before running (${r.denial})` : r.is_error ? "Error" : "Result";
    return el("div", { className: "t-tool" },
      pre(e.id),
      el("div", { className: "t-res", "data-state": !r ? "pending" : r.denial ? "blocked" : r.is_error ? "error" : "ok" },
        el("span", { className: "t-res-h", text: resLabel }),
        r ? pre(r.id) : null));
  }));
  const evIds = it.events.flatMap((e) => (e.result ? [e.id, e.result.id] : [e.id])).join(" ");
  return el("li", { className: "t-turn", "data-ev": evIds },
    details(j, it.request_id || `e${it.events[0].id}`, isLast, summary, body));
}

// Rellena los <pre data-content> visibles (fuera de <details> cerrados) con su texto.
async function fillContent(scope) {
  const targets = [...scope.querySelectorAll("[data-content]")].filter((n) => !n.closest("details:not([open])"));
  const missing = [...new Set(targets.map((n) => Number(n.dataset.content)).filter((id) => !contentCache.has(id)))];
  for (let i = 0; i < missing.length; i += 200) {
    const r = await api(`/api/content?ids=${missing.slice(i, i + 200).join(",")}`);
    if (!r.ok) return;
    for (const [id, c] of Object.entries(await r.json())) contentCache.set(Number(id), c);
  }
  for (const n of targets) {
    const c = contentCache.get(Number(n.dataset.content));
    if (!c) continue;
    n.textContent = c.ok ? c.text : "Content unavailable";   // textContent: nunca HTML (§9)
    n.dataset.state = c.ok ? (c.truncated ? "truncated" : "ok") : "unavailable";
  }
}

// --- F6.5: tema, pestañas, timeline y resúmenes ----------------------------------------------------
// Tema: sigue al sistema hasta que el usuario elige; la elección vive en este navegador.
const THEME_KEY = "traza-theme";
const systemDark = matchMedia("(prefers-color-scheme: dark)");
function storedTheme() { try { return localStorage.getItem(THEME_KEY); } catch { return null; } }
let themeChoice = storedTheme();         // en memoria: vale aunque no se pueda guardar
function applyTheme() {
  const t = themeChoice || (systemDark.matches ? "dark" : "light");
  document.documentElement.dataset.theme = t;
  $("theme").setAttribute("aria-label", t === "dark" ? "Switch to light theme" : "Switch to dark theme");
}
$("theme").addEventListener("click", () => {
  themeChoice = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, themeChoice); } catch { /* sin almacenamiento: solo esta visita */ }
  applyTheme();
});
systemDark.addEventListener("change", applyTheme);
applyTheme();

// Pestañas Tree | Timeline (plan F6.5, punto 1): dos vistas con su propio layout.
for (const b of document.querySelectorAll(".tabs [role=tab]")) {
  b.addEventListener("click", () => selectTab(b.dataset.tab));
  b.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const other = document.querySelector(`.tabs [data-tab="${b.dataset.tab === "tree" ? "trace" : "tree"}"]`);
    selectTab(other.dataset.tab); other.focus();
  });
}
function selectTab(tab) {
  ui.tab = tab;
  for (const b of document.querySelectorAll(".tabs [role=tab]")) {
    const on = b.dataset.tab === tab;
    b.setAttribute("aria-selected", String(on));
    b.tabIndex = on ? 0 : -1;
  }
  renderPanel();
}

const fmtDur = (s) => {
  if (s == null) return "?";
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
};
const fmtWhen = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "?");

// Barra apilada con su leyenda: [[clave, etiqueta, valor, texto]]
function stackBar(label, rows) {
  const total = rows.reduce((a, r) => a + (r[2] || 0), 0);
  const bar = el("div", { className: "stack", role: "img",
    "aria-label": `${label}: ` + rows.map((r) => `${r[1]} ${r[3]}`).join(", ") },
    ...rows.filter((r) => r[2] > 0).map((r) => {
      const s = el("span", { className: `k-${r[0]}`, title: `${r[1]}: ${r[3]}` });
      s.style.flex = `${r[2]} 1 0`;
      return s;
    }));
  const pct = (v) => (total ? `${Math.round((v / total) * 100)} %` : "");
  const legend = el("div", { className: "stack-legend" }, ...rows.map((r) =>
    el("span", {}, el("span", { className: `sw k-${r[0]}` }), `${r[1]} `, el("b", { text: r[3] }), ` ${pct(r[2] || 0)}`)));
  return el("div", { className: "breakdown" }, el("h4", { text: label }), bar, legend);
}

// Desglose del tiempo activo (plan F6.5): modelo / herramientas / tú
function timeBar(b) {
  const bar = stackBar("Where the active time went", [
    ["model", "Model", b.model, fmtDur(b.model)],
    ["tool", "Tools", b.tool, fmtDur(b.tool)],
    ["user", "You", b.user, fmtDur(b.user)]]);
  // heurística, no un hecho (design.md §7.4): dicho donde se ve el número
  bar.title = "Each gap goes to whoever had the turn. A tool still running counts as tool time however long it takes. " +
    "Any other gap of 5+ minutes is treated as a pause, including waiting for you: an assumption that you were " +
    "probably away, not a fact. If you were reading a long plan elsewhere, 'You' will be lower than reality.";
  return bar;
}

// Barra de valor (plan F6.5): en qué se va el valor a precios de la API
function valueBar(v) {
  if (!v.parts) return el("p", { className: "sub", text: "Value at API prices: ? (no request has a known price)" });
  const p = v.parts;
  const head = `Where the value goes · ${money(v.cost, v.unpriced)}` + (v.unpriced ? ` (${v.unpriced} requests without a price not included)` : "");
  return stackBar(head, [
    ["cache_read", "Cache reads", p.cache_read, money(p.cache_read)],
    ["cache_write", "Cache writes", p.cache_write, money(p.cache_write)],
    ["output", "Output", p.output, money(p.output)],
    ["input", "Input", p.input, money(p.input)],
    ...(p.web_search ? [["web_search", "Web search", p.web_search, money(p.web_search)]] : [])]);
}

function facts(cls, pairs) {
  return el("dl", { className: cls }, ...pairs.map(([k, v]) => el("div", {}, el("dt", { text: k }), el("dd", { text: v }))));
}

function renderSummary(sm) {
  if (!sm) return $("j-summary").replaceChildren();
  const tok = (n) => (n == null ? "?" : n.toLocaleString("en-US"));
  $("j-summary").replaceChildren(
    facts("sum-facts", [
      ["Active time", fmtDur(sm.active_s)],
      ["Wall clock", `${fmtWhen(sm.wall_start)} → ${fmtWhen(sm.wall_end)}`],
      ["Model", sm.models.join(", ") || "–"],
      ["Requests", String(sm.requests)],
      ["Tokens in (with cache)", tok(sm.tokens.input)],
      ["Tokens out", tok(sm.tokens.output)]]),
    timeBar(sm.breakdown),
    valueBar(sm.value));
}

// Filas de la timeline en el orden del árbol. Hermanos con el mismo nombre (30 "general-purpose")
// van en UNA fila "×30" que se despliega: 30 etiquetas iguales eran ruido (revisión antes de F7).
const traceOpen = new Set();
function treeRows(s) {
  const kids = new Map();
  for (const n of s.tree) {
    const p = n.orphan ? "(orphan)" : n.parent ?? "(root)";
    if (!kids.has(p)) kids.set(p, []);
    kids.get(p).push(n);
  }
  const out = [];
  const walk = (parent, depth) => {
    const groups = new Map();
    for (const n of kids.get(parent) || []) {
      if (!groups.has(agentName(n))) groups.set(agentName(n), []);
      groups.get(agentName(n)).push(n);
    }
    for (const [name, members] of groups) {
      if (members.length === 1) { out.push({ n: members[0], depth }); walk(members[0].id, depth + 1); continue; }
      const key = `${parent}|${name}`;
      out.push({ group: { key, name, members, open: traceOpen.has(key) }, depth });
      if (traceOpen.has(key)) for (const m of members) { out.push({ n: m, depth: depth + 1, member: true }); walk(m.id, depth + 2); }
    }
  };
  walk("(root)", 0); walk("(orphan)", 0);
  return out;
}

// Una barra continua por agente. Los tramos de menos de MIN_PX se acumulan en una cubeta pintada
// del tipo con más tiempo dentro: 2.000 turnos de segundos en 5 h eran un peine de rayas de 1 px.
const MIN_PX = 1.5;
function binSegments(segs, pxPerS) {
  const minS = MIN_PX / pxPerS, out = [];
  let bin = null;
  const flush = () => {
    if (!bin) return;
    const kind = Object.entries(bin.t).sort((x, y) => y[1] - x[1])[0][0];
    const last = out[out.length - 1];
    if (last && last.kind === kind && bin.start - last.end < minS) last.end = Math.max(last.end, bin.end);
    else out.push({ start: bin.start, end: bin.end, kind, event: bin.event });
    bin = null;
  };
  for (const [st, en, kind, event] of [...segs].sort((x, y) => x[0] - y[0])) {
    if (bin && st - bin.end >= minS) flush();          // hueco visible: la cubeta no lo cruza
    bin ||= { start: st, end: st, t: {}, event };
    bin.end = Math.max(bin.end, en);
    bin.t[kind] = (bin.t[kind] || 0) + (en - st);
    if (bin.end - bin.start >= minS) flush();
  }
  flush();
  return out;
}

let traceSeq = 0, traceSig = "";
async function renderTrace(s) {
  const seq = ++traceSeq;
  const r = await api(`/api/sessions/${encodeURIComponent(s.id)}/timeline`);
  if (seq !== traceSeq || !r.ok) return;
  const t = await r.json();
  if (seq !== traceSeq) return;
  const sig = `${s.id}|${JSON.stringify(t)}`;
  if (sig === traceSig) return;            // nada cambió: conserva foco, hover y tooltips
  traceSig = sig;
  const focused = document.activeElement?.closest?.(".trace-bar");
  const keep = focused && [focused.dataset.agent];
  const ax = t.axis, total = ax.total || 1;
  const pct = (x) => `${Math.max(0, Math.min(100, (x / total) * 100))}%`;
  const idle = ax.breaks.reduce((a, b) => a + b.idle_s, 0);
  $("trace-facts").replaceChildren(...facts("x", [
    ["Active time", fmtDur(ax.total)],
    ["Paused (not drawn)", `${fmtDur(idle)} in ${plural(ax.breaks.length, "pause")}`],
    ["Wall clock", `${fmtWhen(ax.start)} → ${fmtWhen(ax.end)}`]]).children);
  const main = t.agents.main;
  $("trace-breakdown").replaceChildren(...(main ? [timeBar(main.breakdown)] : []));

  const breakTpl = ax.breaks.map((b) => {
    const d = el("div", { className: "trace-break", title: `${fmtDur(b.idle_s)} paused here` });
    d.style.left = pct(b.at);
    return d;
  });
  const KIND = { model: "model", tool: "tools", user: "you" };
  const fills = [];                       // [pista, tramos, agente | null, texto] — se pintan al medir
  const rows = treeRows(s).map(({ n, group, depth, member }) => {
    const track = el("div", { className: "trace-track" });
    let label;
    if (group) {
      label = el("button", { type: "button", className: "trace-label trace-group", "aria-expanded": String(group.open),
        text: `${group.name} ×${group.members.length}` });
      label.dataset.key = group.key;
      label.addEventListener("click", () => {
        traceOpen[group.open ? "delete" : "add"](group.key);
        traceSig = "";
        renderTrace(s).then(() => $("trace-chart").querySelector(`.trace-group[data-key="${CSS.escape(group.key)}"]`)?.focus());
      });
      fills.push([track, group.members.flatMap((m) => t.agents[m.id]?.segments || []), null, group.name]);
    } else {
      const name = agentName(n);
      const text = member && n.description ? n.description : name;   // desplegado: 30 nombres iguales no dicen nada
      label = el("div", { className: "trace-label", text, title: n.description || name });
      fills.push([track, t.agents[n.id]?.segments || [], n.id, text]);
    }
    label.style.setProperty("--depth", depth);
    return el("div", { className: "trace-row" }, label, track);
  });
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const sp = el("span", { text: fmtDur(ax.total * f) });
    sp.style.left = `${f * 100}%`;
    if (f === 1) sp.style.transform = "translateX(-100%)";
    return sp;
  });
  $("trace-chart").replaceChildren(...rows,
    el("div", { className: "trace-axis" }, el("span", { text: "Active time →" }), el("div", { className: "trace-ticks" }, ...breakTpl, ...ticks)));
  // tras montar: el ancho real de la pista decide qué es sub-píxel
  const pxPerS = (fills[0]?.[0].clientWidth || 600) / total;
  for (const [track, segs, agent, text] of fills) {
    if (!segs.length) continue;
    const bins = binSegments(segs, pxPerS);
    const b0 = bins[0].start, span = Math.max(bins[bins.length - 1].end - b0, 1e-9);
    const sum = { model: 0, tool: 0, user: 0 };
    for (const [st, en, k] of segs) sum[k] += en - st;
    const desc = Object.entries(sum).filter(([, v]) => v).map(([k, v]) => `${KIND[k]} ${fmtDur(v)}`).join(", ");
    const bar = el(agent ? "button" : "div", { className: "trace-bar" });
    if (agent) {
      bar.type = "button";
      bar.dataset.agent = agent;
      bar.setAttribute("aria-label", `${text}: ${desc}. Opens the turn under the pointer`);
      // un botón por agente; el clic abre el turno bajo el puntero (con teclado: el primero)
      bar.addEventListener("click", (e) => {
        const r = bar.getBoundingClientRect();
        const at = b0 + (Math.max(0, e.clientX - r.left) / r.width) * span;
        const hit = bins.find((x) => x.end >= at) || bins[bins.length - 1];
        openJudge(s.id, agent, hit.event);
      });
    } else bar.title = desc;
    bar.style.left = pct(b0);
    bar.style.width = pct(span);
    for (const x of bins) {
      const sp = el("span", { className: `seg seg-${x.kind}` });
      sp.style.left = `${((x.start - b0) / span) * 100}%`;
      sp.style.width = `${((x.end - x.start) / span) * 100}%`;
      bar.append(sp);
    }
    track.append(bar);
  }
  if (keep) $("trace-chart").querySelector(`.trace-bar[data-agent="${CSS.escape(keep[0])}"]`)?.focus();
}

// el ancho de la pista decide qué tramos son sub-píxel: al cambiar el tamaño, se vuelven a agrupar
let traceResize = null;
addEventListener("resize", () => {
  clearTimeout(traceResize);
  traceResize = setTimeout(() => { if (!$("trace").hidden) { traceSig = ""; renderPanel(); } }, 200);
});
