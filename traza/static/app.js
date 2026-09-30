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
  $("live-sessions").textContent = d.counts.live_sessions;
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

async function renderPanel() {
  const body = $("panel-body");
  if (!ui.selected) {
    body.hidden = true;
    $("panel-empty").hidden = false;
    return;
  }
  const wanted = ui.selected;
  const r = await fetch(`/api/sessions/${encodeURIComponent(wanted)}`);
  if (wanted !== ui.selected) return;   // otro clic llegó antes: no pintar una sesión vieja
  if (!r.ok) {                     // la sesión desapareció del disco (limpieza de 30 días)
    ui.selected = null;
    return renderPanel();
  }
  const s = await r.json();
  if (wanted !== ui.selected) return;
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
  const r = await fetch("/api/overview");
  ui.data = await r.json();
  renderCards(ui.data);
  renderRows(ui.data);
  if (ui.selected) renderPanel();
  return ui.data;
}
function refreshSoon() {                     // varios ticks seguidos → una sola petición
  clearTimeout(pending);
  pending = setTimeout(refresh, 150);
}

function connect(gen) {
  const conn = $("conn");
  const es = new EventSource(`/events?gen=${encodeURIComponent(gen)}`);
  es.addEventListener("hello", () => {           // también tras reconectar: pudo cambiar algo
    conn.dataset.state = "open";
    $("conn-text").textContent = "Watching";
    refreshSoon();
  });
  es.addEventListener("tick", refreshSoon);
  es.addEventListener("reload", () => location.reload());   // la caché se reconstruyó
  es.onerror = () => { conn.dataset.state = "lost"; $("conn-text").textContent = "Reconnecting"; };
}

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
    $("conn").dataset.state = "lost";
    $("conn-text").textContent = "Server not reachable, retrying";
    setTimeout(start, 2000);
  }
})();
