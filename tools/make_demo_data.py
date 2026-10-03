"""Datos de demo para traza (GIF, capturas): sesiones de Claude Code sintéticas pero creíbles,
con el formato real de los JSONL, sin tocar ~/.claude. No son las fixtures de los tests (mínimas,
escritas a mano, una por caso): esto genera un disco de ejemplo entero.

    python tools/make_demo_data.py demo                  # escribe demo/projects/...
    python -m traza serve --root demo/projects --db demo/traza.db
    python tools/make_demo_data.py demo --live --wait 5  # y además, dos subagentes en vivo (~15 s)

La sesión principal tiene la forma que hace útil el Flame: 25 agentes y algo más de la mitad del
valor en los subagentes (como 5a1f386d en disco; findings.md "Flame"). Determinista (semilla fija),
salvo las horas, que se calculan desde ahora para que la sesión principal salga "en vivo".
"""
import argparse
import json
import os
import random
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "2.1.300"
NOW = datetime.now(timezone.utc)

FILES = ["src/billing/client.ts", "src/billing/charges.ts", "src/billing/refunds.ts",
         "src/events/publisher.ts", "src/events/schema/invoice.ts", "src/webhooks/stripe.ts",
         "src/jobs/reconcile.ts", "test/billing/charges.test.ts", "docs/adr/0012-event-queue.md"]
PATTERNS = ["BillingClient\\.", "invoice\\.created", "retryWithBackoff", "publishEvent\\(",
            "idempotencyKey", "chargeCustomer"]
COMMANDS = [("npm test -- billing", "Run billing tests"), ("git diff --stat", "Show changed files"),
            ("npx tsc --noEmit", "Type-check"), ("npm run lint -- src/billing", "Lint billing")]
SAYS = ["Let me look at how charges are created today.", "The refund path calls the HTTP client directly too.",
        "I'll move this behind the publisher and keep the old call behind the flag.",
        "Tests pass locally; checking the reconciliation job next.",
        "Two consumers still expect the old payload shape.", "Updating the schema and the handler together."]


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hexid(n: int, rnd: random.Random) -> str:
    return "".join(rnd.choice("0123456789abcdef") for _ in range(n))


def meta(path: Path, kind: str, desc: str, tool_use_id: str) -> None:
    path.with_suffix(".meta.json").write_text(json.dumps(
        {"agentType": kind, "description": desc, "toolUseId": tool_use_id, "spawnDepth": 1,
         "requestShape": "foreground", "requestNonInteractive": True}), encoding="utf-8")


class Log:
    """Un fichero JSONL de Claude Code: el de una sesión o el de un subagente."""

    def __init__(self, path: Path, sid: str, cwd: str, rnd: random.Random, agent_id: str | None = None):
        self.path, self.sid, self.cwd, self.rnd, self.agent_id = path, sid, cwd, rnd, agent_id
        path.parent.mkdir(parents=True, exist_ok=True)

    def raw(self, d: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(d) + "\n")

    def line(self, t: datetime, **d) -> None:
        base = {"parentUuid": None, "uuid": str(uuid.UUID(int=self.rnd.getrandbits(128))),
                "timestamp": iso(t), "isSidechain": self.agent_id is not None,
                "userType": "external", "entrypoint": "cli", "cwd": self.cwd,
                "sessionId": self.sid, "version": VERSION, "gitBranch": "main"}
        if self.agent_id:
            base["agentId"] = self.agent_id
        self.raw({**base, **d})

    def prompt(self, t: datetime, text: str) -> None:
        self.line(t, type="user", message={"role": "user", "content": text})
        self.hook(t, "UserPromptSubmit")

    # Líneas que traza ignora adrede, como en un disco real (el más frecuente: attachment). Sin
    # ellas la barra de salud diría "0 lines ignored" y parecería que no se descarta nada.
    def hook(self, t: datetime, event: str) -> None:
        self.line(t + timedelta(milliseconds=120), type="attachment", attachment={
            "type": "hook_success", "hookName": event, "content": "", "stdout": "", "exitCode": 0})

    def snapshot(self, t: datetime) -> None:
        self.raw({"type": "file-history-snapshot", "messageId": str(uuid.UUID(int=self.rnd.getrandbits(128))),
                  "snapshot": {"trackedFileBackups": {}, "timestamp": iso(t)}, "isSnapshotUpdate": False})

    def reply(self, t: datetime, model: str, blocks: list, ctx: int, out: int, stop="tool_use") -> datetime:
        """Una petición a la API: un bloque por línea, todas con el mismo requestId y usage."""
        rid, mid = "req_" + hexid(24, self.rnd), "msg_" + hexid(24, self.rnd)
        write = self.rnd.randint(ctx // 40, ctx // 12)
        usage = {"input_tokens": self.rnd.randint(3, 12), "cache_creation_input_tokens": write,
                 "cache_read_input_tokens": ctx, "output_tokens": out,
                 "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
                 "service_tier": "standard",
                 "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": write},
                 "inference_geo": "not_available", "speed": "standard"}
        for i, b in enumerate(blocks):
            t += timedelta(seconds=self.rnd.uniform(1.5, 6))
            self.line(t, type="assistant", requestId=rid, message={
                "model": model, "id": mid, "type": "message", "role": "assistant", "content": [b],
                "stop_reason": stop if i == len(blocks) - 1 else None, "stop_sequence": None,
                "usage": usage})
        return t

    def results(self, t: datetime, items: list, agent: str | None = None) -> None:
        """Resultados de herramientas; `agent`: el subagente que terminó (toolUseResult)."""
        extra = {"toolUseResult": {"status": "completed", "agentId": agent}} if agent else {}
        self.line(t, type="user", message={"role": "user", "content": [
            {"type": "tool_result", "content": c, "is_error": False, "tool_use_id": tid}
            for tid, c in items]}, **extra)

    def work(self, t: datetime, model: str, turns: int, ctx: int, out_max: int) -> datetime:
        """`turns` peticiones de trabajo normal: a veces texto, y una herramienta con su resultado."""
        r = self.rnd
        for _ in range(turns):
            # dos llamadas seguidas nunca iguales: tres idénticas son la señal de bucle, y un bucle
            # inventado por azar sería mentir en la demo
            while True:
                name = r.choice(["Read", "Read", "Grep", "Edit", "Bash"])
                inp = {"Read": lambda: {"file_path": f"{self.cwd}/{r.choice(FILES)}"},
                       "Grep": lambda: {"pattern": r.choice(PATTERNS), "path": "src/"},
                       "Edit": lambda: {"file_path": f"{self.cwd}/{r.choice(FILES)}",
                                        "old_string": "await billing.charge(",
                                        "new_string": "await publishEvent(\"charge.requested\", "},
                       "Bash": lambda: dict(zip(("command", "description"), r.choice(COMMANDS)))}[name]()
                if (name, inp) != getattr(self, "last", None):
                    break
            self.last = name, inp
            tid = "toolu_" + hexid(24, r)
            blocks = ([{"type": "text", "text": r.choice(SAYS)}] if r.random() < 0.4 else []) + \
                     [{"type": "tool_use", "id": tid, "name": name, "input": inp}]
            ctx = int(ctx * r.uniform(1.0, 1.06)) + 800
            t = self.reply(t, model, blocks, ctx, r.randint(out_max // 6, out_max))
            t += timedelta(seconds=r.uniform(1, 25 if name == "Bash" else 4))
            self.results(t, [(tid, "ok" if name == "Edit" else "(output)")])
            if name == "Edit":
                self.snapshot(t)
        return t


def subagent(d: Path, main: Log, t: datetime, kind: str, desc: str, turns: int) -> tuple[str, str, datetime]:
    """Un subagente con su fichero y su meta.json. Devuelve (tool_use_id, agent_id, fin)."""
    r = main.rnd
    aid, tid = "a" + hexid(16, r), "toolu_" + hexid(24, r)
    sub = d / main.sid / "subagents" / f"agent-{aid}.jsonl"
    log = Log(sub, main.sid, main.cwd, r, agent_id=aid)
    meta(sub, kind, desc, tid)
    log.prompt(t, f"{desc}. Report file:line references and anything that looks risky.")
    model = "claude-haiku-4-5-20251001" if kind == "Explore" else "claude-sonnet-5"
    t = log.work(t, model, turns, ctx=r.randint(14000, 26000), out_max=2400)
    t = log.reply(t, model, [{"type": "text", "text": f"Done: {desc.lower()}."}], 30000,
                  r.randint(300, 900), stop="end_turn")
    return tid, aid, t


def wave(d: Path, main: Log, t: datetime, model: str, ctx: int, agents: list) -> tuple[datetime, int]:
    """El principal lanza varios subagentes en paralelo y espera a todos."""
    r = main.rnd
    runs = [subagent(d, main, t + timedelta(seconds=5), kind, desc, turns) for kind, desc, turns in agents]
    blocks = [{"type": "tool_use", "id": tid, "name": "Agent",
               "input": {"subagent_type": kind, "description": desc, "prompt": desc}}
              for (tid, _, _), (kind, desc, _) in zip(runs, agents)]
    main.reply(t, model, [{"type": "text", "text": f"Splitting this into {len(agents)} parallel tasks."}] + blocks,
               ctx, r.randint(900, 2200))
    for tid, aid, end in sorted(runs, key=lambda x: x[2]):
        main.results(end, [(tid, f"Agent {aid} finished.")], agent=aid)
    return max(e for *_, e in runs) + timedelta(seconds=3), int(ctx * 1.15)


def session(root: Path, proj: str, title: str, start: datetime, ask: str, rnd: random.Random,
            plan: list, model="claude-opus-5-5") -> tuple[Log, datetime]:
    """Una sesión: `plan` es una lista de ("work", turnos) o ("agents", [(tipo, descripción, turnos)])."""
    sid = str(uuid.UUID(int=rnd.getrandbits(128)))
    d = root / f"-home-dev-code-{proj}"
    main = Log(d / f"{sid}.jsonl", sid, f"/home/dev/code/{proj}", rnd)
    main.raw({"type": "ai-title", "aiTitle": title, "sessionId": sid})
    for op in ("enqueue", "dequeue"):
        main.raw({"type": "queue-operation", "operation": op, "timestamp": iso(start), "sessionId": sid})
    main.prompt(start, ask)
    t, ctx = start, 42000
    for step, arg in plan:
        if step == "work":
            t = main.work(t, model, arg, ctx, out_max=1600)
            ctx = int(ctx * 1.2)
        else:
            t, ctx = wave(d, main, t, model, ctx, arg)
    t = main.reply(t, model, [{"type": "text", "text": "Done. Summary of the changes is above."}],
                   ctx, 700, stop="end_turn")
    main.line(t, type="system", subtype="stop_hook_summary", hookCount=1, hookErrors=[],
              preventedContinuation=False, level="suggestion")
    return main, t


E, G = "Explore", "general-purpose"
OLD = [
    ("web-app", "Investigate flaky checkout test", 30,
     "The checkout e2e test fails about one run in ten on CI. Find out why.",
     [("work", 9), ("agents", [(E, "Find timing assumptions in checkout tests", 5),
                               (G, "Reproduce the failure 20 times", 8)]), ("work", 6)]),
    ("data-pipeline", "Add retries to ingestion job", 52,
     "The nightly ingestion job dies on the first S3 timeout. Add retries with backoff.",
     [("work", 12), ("agents", [(G, "Write tests for the retry policy", 7)]), ("work", 4)]),
    ("api-gateway", "Refactor auth middleware", 75,
     "Split the auth middleware into token parsing and policy checks.",
     [("work", 8), ("agents", [(E, "Map routes that bypass auth", 4), (E, "List policy checks per route", 4),
                               (G, "Draft the new middleware", 9)]), ("work", 10)]),
    ("platform-docs", "Write ADR for caching layer", 100,
     "Write an ADR comparing Redis and in-process caching for the catalog.", [("work", 7)]),
]
BIG = [
    ("work", 9),
    ("agents", [(E, "Map every call to BillingClient", 6), (E, "Find consumers of invoice.created", 4),
                (E, "Locate retry and backoff helpers", 3), (E, "Survey existing queue adapters", 5)]),
    ("work", 10),
    ("agents", [(G, "Draft the invoice event schema", 9), (G, "Port the charge flow to the publisher", 14),
                (G, "Port the refund flow to the publisher", 12), (G, "Update the Stripe webhook handler", 10),
                (G, "Write the invoice.paid consumer", 13), (G, "Add idempotency keys to events", 8)]),
    ("work", 11),
    ("agents", [(G, "Migrate the nightly reconciliation job", 11), (G, "Backfill script for open invoices", 9),
                (G, "Update the OpenAPI spec", 4), (G, "Fix failing billing tests", 15),
                (E, "Check PII in event payloads", 5), (G, "Load-test the consumer", 7),
                (G, "Add metrics to the publisher", 6)]),
    ("work", 9),
    ("agents", [("Plan", "Plan the rollout behind a feature flag", 5), (G, "Review the billing diff", 8),
                (G, "Write the cut-over runbook", 6), (E, "Find dead code after the migration", 4),
                (G, "Update integration tests", 10), (G, "Document the event contracts", 5),
                (G, "Update the changelog", 3)]),
    ("work", 7),
]


DEMO_ENTRIES = {"projects", "traza.db", "traza.db-wal", "traza.db-shm"}


def build(out: Path) -> Log:
    # solo se borra una carpeta que contiene lo que este generador escribe: `make_demo_data.py .`
    # se llevaría el repositorio, y `~` la carpeta personal (/code-review de F7)
    if out.exists():
        extra = {p.name for p in out.iterdir()} - DEMO_ENTRIES
        if extra:
            raise SystemExit(f"{out} contains {sorted(extra)[:3]}…: not a demo folder; pick an empty or new one")
        shutil.rmtree(out)
    root, rnd = out / "projects", random.Random(7)
    for proj, title, hours_ago, ask, plan in OLD:
        s, end = session(root, proj, title, NOW - timedelta(hours=hours_ago), ask, rnd, plan)
        for f in [s.path, *s.path.parent.glob(f"{s.sid}/subagents/*.jsonl")]:
            os.utime(f, (end.timestamp(),) * 2)          # "hace N h" en la lista
    # la sesión principal acaba hace un minuto: en vivo para el panel
    main, end = session(root, "web-app", "Migrate billing to the event queue",
                        NOW - timedelta(minutes=110), "Move invoice and charge events from direct HTTP "
                        "calls to the event queue. Keep the old path behind a flag until we cut over.", rnd, BIG)
    shift = (NOW - timedelta(minutes=1)) - end
    for f in [main.path, *main.path.parent.glob(f"{main.sid}/subagents/*.jsonl")]:
        lines = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
        for d in lines:
            if "timestamp" in d:
                d["timestamp"] = iso(datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")) + shift)
        f.write_text("".join(json.dumps(d) + "\n" for d in lines), encoding="utf-8")
    return main


def live(main: Log) -> None:
    """Lo que sale en el GIF: el principal lanza dos subagentes que escriben en vivo y terminan."""
    r, model = main.rnd, "claude-opus-5-5"
    now = lambda: datetime.now(timezone.utc)
    jobs = [(E, "Verify no direct BillingClient calls remain", 4, "claude-haiku-4-5-20251001"),
            (G, "Run the full billing test suite", 6, "claude-sonnet-5")]
    ids = [("toolu_" + hexid(24, r), "a" + hexid(16, r)) for _ in jobs]
    main.reply(now() - timedelta(seconds=4), model,
               [{"type": "tool_use", "id": tid, "name": "Agent",
                 "input": {"subagent_type": k, "description": d, "prompt": d}}
                for (tid, _), (k, d, *_) in zip(ids, jobs)], 220000, 1200)
    logs = []
    for (tid, aid), (kind, desc, *_) in zip(ids, jobs):
        sub = main.path.parent / main.sid / "subagents" / f"agent-{aid}.jsonl"
        log = Log(sub, main.sid, main.cwd, r, agent_id=aid)
        meta(sub, kind, desc, tid)
        log.prompt(now(), f"{desc}. Report anything that still needs a change.")
        logs.append(log)
    left = [turns for _, _, turns, _ in jobs]
    while any(left):
        time.sleep(1.2)
        for i, log in enumerate(logs):
            if not left[i]:
                continue
            m = jobs[i][3]
            log.work(now() - timedelta(seconds=30), m, 1, ctx=180000 + 20000 * (7 - left[i]), out_max=6000)
            left[i] -= 1
            if not left[i]:
                log.reply(now() - timedelta(seconds=6), m, [{"type": "text", "text": "Done."}], 60000, 600,
                          stop="end_turn")
                main.results(now(), [(ids[i][0], "Agent finished.")], agent=ids[i][1])
    time.sleep(1.5)
    main.reply(now() - timedelta(seconds=6), model,
               [{"type": "text", "text": "Both checks pass. The migration is ready for review."}],
               230000, 800, stop="end_turn")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path, help="carpeta de salida (se borra y se rehace)")
    ap.add_argument("--live", action="store_true", help="después, dos subagentes en vivo")
    ap.add_argument("--wait", type=float, default=0, help="segundos antes de empezar lo en vivo")
    args = ap.parse_args()
    main = build(args.out)
    print(f"demo: traza serve --root {args.out / 'projects'} --db {args.out / 'traza.db'}", flush=True)
    if args.live:
        time.sleep(args.wait)
        live(main)
