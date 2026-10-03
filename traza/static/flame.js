// Flame (pestaña Flame): el ancho de cada agente es su valor a precios de la API, el propio más
// el de sus descendientes. Función pura, sin DOM (la prueban tests/flame.test.mjs).
//   x, w: fracciones del valor de la sesión (0..1)   y: fila (0 = la sesión, 1 = sus agentes…)
//   kind: "root" | "agent" | "own" (el hueco que dejan los hijos: coste propio del padre) |
//         "more" (n agentes agregados: por debajo de minFrac o más hondos que maxDepth)
// Un agente sin precio cuenta 0 (el panel ya lo marca con "+" en el árbol).
export function flameLayout(tree, { maxDepth = 8, minFrac = 0.004 } = {}) {
  const ids = new Set(tree.map((n) => n.id));
  const kids = new Map([[null, []]]);
  for (const n of tree) {
    // un huérfano no tiene padre (db.orphans: su tool_use no está): cuelga de la sesión
    const p = n.parent != null && ids.has(n.parent) ? n.parent : null;
    if (!kids.has(p)) kids.set(p, []);
    kids.get(p).push(n);
  }
  // `children`: los hijos de verdad, recorriendo desde la sesión sin repetir. Lo que no se
  // alcanza (un ciclo de padres: meta.json mal o manipulado) cuelga de la sesión, como un
  // huérfano: si no, su valor desaparecería del total sin aviso.
  const sub = new Map(), count = new Map(), children = new Map([[null, []]]);
  const walk = (n, parent) => {
    sub.set(n.id, 0);                                   // visitado
    children.get(parent).push(n);
    children.set(n.id, []);
    let s = n.cost || 0, c = 1;
    for (const k of kids.get(n.id) || []) {
      if (sub.has(k.id)) continue;
      walk(k, n.id); s += sub.get(k.id); c += count.get(k.id);
    }
    sub.set(n.id, s); count.set(n.id, c);
  };
  kids.get(null).forEach((n) => walk(n, null));
  for (const n of tree) if (!sub.has(n.id)) walk(n, null);
  const total = children.get(null).reduce((a, n) => a + sub.get(n.id), 0);
  if (!(total > 0)) return [];

  const rects = [{ kind: "root", x: 0, y: 0, w: 1, h: 1, agent_id: null, parent_id: null, cost: total }];
  // hijos de `parent` en la fila `y`, desde `x`; detrás, lo agregado y el coste propio del padre
  const place = (parent, x, y) => {
    const placed = children.get(parent?.id ?? null).filter((k) => sub.get(k.id) > 0)
      .sort((a, b) => sub.get(b.id) - sub.get(a.id));
    const pid = parent?.id ?? null;
    let more = null;
    for (const k of placed) {
      const w = sub.get(k.id) / total;
      if (y > maxDepth || w < minFrac) {
        more ||= { kind: "more", y, h: 1, agent_id: null, parent_id: pid, cost: 0, n: 0, ids: [] };
        more.cost += sub.get(k.id);
        more.n += count.get(k.id);
        more.ids.push(k.id);                  // los agregados, para la leyenda (sus hijos van dentro)
        continue;
      }
      rects.push({ kind: "agent", x, y, w, h: 1, agent_id: k.id, parent_id: pid, cost: sub.get(k.id), own: k.cost || 0 });
      place(k, x, y + 1);
      x += w;
    }
    if (more) { rects.push({ ...more, x, w: more.cost / total }); x += more.cost / total; }
    if (parent && placed.length && parent.cost > 0) {
      rects.push({ kind: "own", x, y, w: parent.cost / total, h: 1, agent_id: null, parent_id: pid, cost: parent.cost });
    }
  };
  place(null, 0, 1);
  return rects;
}
