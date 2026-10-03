// Layout del flame (pestaña Flame): función pura árbol → rectángulos. La lanza test_flame.py
// (`node --test`); FLAME_TREE trae el árbol real de la fixture, servido por la API.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { flameLayout } from "../traza/static/flame.js";

const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} ≠ ${b}`);
const byId = (rects) => Object.fromEntries(rects.filter((r) => r.agent_id).map((r) => [r.agent_id, r]));
const node = (id, cost, parent = null) => ({ id, cost, parent, orphan: null });

test("raíz sola: 100 %", () => {
  const r = flameLayout([node("main", 10)]);
  const root = r.find((x) => x.kind === "root");
  assert.equal(root.w, 1);
  near(byId(r).main.w, 1);
  assert.equal(r.filter((x) => x.kind === "own").length, 0);      // una hoja es todo coste propio
});

test("dos hijos 1:1 sin coste propio: 50 / 50", () => {
  const r = byId(flameLayout([node("main", 0), node("a", 1, "main"), node("b", 1, "main")]));
  near(r.a.w, 0.5); near(r.b.w, 0.5);
  near(r.a.x, 0); near(r.b.x, 0.5);
});

test("dos hijos 1:1 con coste propio: 1/3 + 1/3 + 1/3", () => {
  const rects = flameLayout([node("main", 1), node("a", 1, "main"), node("b", 1, "main")]);
  const r = byId(rects);
  near(r.a.w, 1 / 3); near(r.b.w, 1 / 3);
  const own = rects.filter((x) => x.kind === "own");
  assert.equal(own.length, 1);
  near(own[0].w, 1 / 3);
  assert.equal(own[0].parent_id, "main");
  assert.equal(own[0].y, r.a.y);                                      // en la fila de los hijos
});

test("tres niveles: cada hijo bajo su padre, una fila más abajo", () => {
  const r = byId(flameLayout([node("main", 2), node("a", 1, "main"), node("a1", 1, "a")]));
  near(r.main.w, 1); near(r.a.w, 0.5); near(r.a1.w, 0.25);
  assert.deepEqual([r.main.y, r.a.y, r.a1.y], [1, 2, 3]);
  assert.ok(r.a1.x >= r.a.x && r.a1.x + r.a1.w <= r.a.x + r.a.w + 1e-9);
});

test("padre con un solo hijo y sin coste propio: mismo ancho", () => {
  const r = byId(flameLayout([node("main", 0), node("a", 3, "main")]));
  near(r.a.w, 1); near(r.a.x, 0);
});

test("profundidad excedida: un rectángulo 'N more' con todo lo de debajo", () => {
  const tree = [node("main", 1), node("a", 1, "main"), node("b", 1, "a"), node("c", 1, "b")];
  const rects = flameLayout(tree, { maxDepth: 2 });                  // main y a; b y c no caben
  const more = rects.filter((x) => x.kind === "more");
  assert.equal(more.length, 1);
  assert.equal(more[0].n, 2);                                          // b y su hijo c
  assert.deepEqual(more[0].ids, ["b"]);                                // la leyenda lista b (c va dentro)
  near(more[0].w, 0.5);
  assert.ok(!byId(rects).b && !byId(rects).c);
});

test("hermanos por debajo del ancho mínimo se agregan en 'N more'", () => {
  const tree = [node("main", 0), node("big", 98, "main"), node("s1", 1, "main"), node("s2", 1, "main")];
  const rects = flameLayout(tree, { minFrac: 0.05 });
  const more = rects.filter((x) => x.kind === "more");
  assert.equal(more.length, 1);
  assert.equal(more[0].n, 2);
  assert.deepEqual(more[0].ids, ["s1", "s2"]);                         // los que no caben, para la leyenda
  near(more[0].w, 0.02);
});

test("huérfanos y principal cuelgan de la sesión; sin coste → nada que dibujar", () => {
  const r = byId(flameLayout([node("main", 3), { id: "o", cost: 1, parent: null, orphan: "sin_tool_use_id" }]));
  near(r.main.w, 0.75); near(r.o.w, 0.25);
  assert.deepEqual(flameLayout([node("main", 0)]), []);
  near(byId(flameLayout([node("main", null), node("a", 2, "main")])).a.w, 1);   // sin precio = 0
});

test("un ciclo de padres (meta.json mal o manipulado) no hace desaparecer agentes", () => {
  // /code-review: lo que no se alcanzaba bajando desde la sesión se perdía del total
  const self = flameLayout([node("main", 5), node("x", 5, "x")]);
  near(self.find((r) => r.kind === "root").cost, 10);
  near(byId(self).x.w, 0.5);
  const cycle = [node("main", 4), node("a", 3, "b"), node("b", 3, "a")];
  const rects = flameLayout(cycle);
  near(rects.find((r) => r.kind === "root").cost, 10);
  invariants(cycle, rects);
});

// Invariantes sobre un árbol real (la fixture servida por la API): todo cabe y suma el total.
function invariants(tree, rects) {
  const total = tree.reduce((a, n) => a + (n.cost || 0), 0);
  const root = rects.find((x) => x.kind === "root");
  near(root.cost, total);
  // el área "terminal" (hojas + coste propio + agregados) es el 100 % exacto
  const parents = new Set(rects.filter((x) => x.kind !== "root").map((x) => x.parent_id));
  const terminal = rects.filter((x) => x.kind === "own" || x.kind === "more"
    || (x.kind === "agent" && !parents.has(x.agent_id)));
  near(terminal.reduce((a, x) => a + x.w, 0), 1);
  for (const x of rects) {
    if (x.kind === "root") continue;
    const p = x.parent_id ? rects.find((r) => r.agent_id === x.parent_id) : root;
    assert.ok(x.x >= p.x - 1e-9 && x.x + x.w <= p.x + p.w + 1e-9, `${x.agent_id ?? x.kind} se sale de su padre`);
    assert.equal(x.y, p.y + 1);
  }
}

test("árbol real: suma 100 %, nada se sale de su padre, raíz = total de la sesión", { skip: !process.env.FLAME_TREE }, () => {
  const tree = JSON.parse(readFileSync(process.env.FLAME_TREE, "utf8"));
  assert.ok(tree.length >= 2);
  invariants(tree, flameLayout(tree));
  invariants(tree, flameLayout(tree, { maxDepth: 1, minFrac: 0.3 }));  // también con agregados
});
