// traza — pantalla de sesiones (F3). Todo texto que viene de los JSONL se pinta con
// textContent: nunca se interpreta como HTML (design.md §9).

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";   // identificador del estándar, no una petición
const STATE_LABEL = { tool: "Running tool", thinking: "Thinking", idle: "Idle" };
const ui = { data: null, filter: "all", selected: null };

// --- formato ---------------------------------------------------------------------------------
function money(v, unpriced = 0) {
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
      ? `${s.activity.agent} · ${s.activity.tool} · ${since(s.activity.since)}`
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
    `costs are estimates from public prices`;
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
        s.errors ? el("span", { className: "badge", title: `${s.errors} results marked as error (hook blocks included) or API errors` },
          icon(ALERT), el("span", { text: String(s.errors) })) : null),
      el("span", { className: "row-sub", text: `${s.project} · ${shortId(s.id)}` }),
      el("span", { className: "row-right" },
        el("span", { className: "row-cost", text: money(s.cost, s.unpriced) }),
        el("span", { className: "row-meta" }, pill(s.state), el("span", { text: ago(s.last_activity) }))),
      ...s.inherits.map((i) => el("span", {
        className: "inherit-line",
        text: `inherits ${i.requests.toLocaleString("en-US")} requests from ${shortId(i.from)} (+${money(i.cost)})`,
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
  const r = await fetch(`/api/sessions/${encodeURIComponent(wanted)}`);
  if (seq !== panelSeq) return;   // llegó una petición más nueva: no pintar datos viejos
  if (!r.ok) {                     // la sesión desapareció del disco (limpieza de 30 días)
    ui.selected = null;
    return renderPanel();
  }
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
    `Also contains ${i.requests.toLocaleString("en-US")} requests owned by ${shortId(i.from)} (+${money(i.cost)}), not counted here`).join(" · ");
  $("p-started").textContent = s.started ? new Date(s.started).toLocaleString() : "?";
  $("p-last").textContent = ago(s.last_activity);
  $("p-requests").textContent = s.requests.toLocaleString("en-US");
  $("p-agents").textContent = String(s.agents);
  $("p-models").replaceChildren(...Object.entries(s.models).map(([m, n]) =>
    el("li", {}, el("span", { text: m }), el("span", { className: "n", text: `${n.toLocaleString("en-US")} ${n === 1 ? "request" : "requests"}` }))));
}

function select(id) {
  ui.selected = id;
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
  const r = await fetch("/api/overview");
  const data = await r.json();
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
const CONN_TEXT = { open: "Watching", reconnecting: "Reconnecting…", lost: "Disconnected · data may be stale" };
const live = { es: null, gen: null, lastSeen: 0, downSince: 0 };

function setConn(state) {
  $("conn").dataset.state = state;
  $("conn-text").textContent = CONN_TEXT[state];
}
function connect(gen) {
  live.gen = gen;
  live.es?.close();
  const es = (live.es = new EventSource(`/events?gen=${encodeURIComponent(gen)}`));
  const alive = () => { live.lastSeen = Date.now(); live.downSince = 0; setConn("open"); };
  es.addEventListener("hello", () => { alive(); refreshSoon(); });   // tras reconectar: pudo cambiar algo
  es.addEventListener("tick", () => { alive(); refreshSoon(); });
  es.addEventListener("ping", alive);
  es.addEventListener("reload", () => location.reload());          // la caché se reconstruyó
  es.onerror = () => {
    live.downSince ||= Date.now();
    // cada reintento fallido vuelve a llamar aquí: no bajar de "lost" a "reconnecting" (parpadeo)
    if (Date.now() - live.downSince <= LOST_AFTER_MS) setConn("reconnecting");
    if (es.readyState === EventSource.CLOSED) setTimeout(() => connect(live.gen), 2000);
  };
}
setInterval(() => {                                    // vigilante
  if (!live.es) return;
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
