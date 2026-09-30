"""Comando de verificación de F1: python -m traza.report <sesión.jsonl>

Imprime agentes, peticiones, tokens y coste estimado por modelo de una sesión (fichero principal
y sus subagentes). Solo lee.
"""
import sys
from collections import Counter
from pathlib import Path

from .parser import Parsed, dedupe_requests, parse_line, parse_meta, tokens_by_model
from .pricing import request_cost


def parse_file(path) -> list[Parsed]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return [parse_line(line) for line in f]


def _files(main_file: Path) -> list:
    """[(agent_id, meta, ruta)] del fichero principal y sus subagentes."""
    files = [("main", None, main_file)]
    for sub in sorted((main_file.parent / main_file.stem / "subagents").glob("agent-*.jsonl")):
        meta_path = sub.with_suffix(".meta.json")
        meta = parse_meta(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
        files.append((sub.stem.removeprefix("agent-"), meta, sub))
    return files


def session_agents(main_file) -> list:
    """[(agent_id, meta, {requestId: Request})] leyendo los ficheros, sin caché: el oráculo con el
    que se compara el coste por nodo del árbol (F4)."""
    return [(aid, meta, dedupe_requests(p.request for p in parse_file(path) if p.request))
            for aid, meta, path in _files(Path(main_file))]


def _cost(reqs) -> str:
    costs = [request_cost(r) for r in reqs]
    return "?" if None in costs else f"${sum(costs):.6f}"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    main_file = Path(argv[0])
    files = _files(main_file)

    all_reqs = {}
    ignored, unknown = Counter(), 0
    print(f"Sesión {main_file.stem}")
    for agent_id, meta, path in files:
        parsed = parse_file(path)
        reqs = dedupe_requests(p.request for p in parsed if p.request)
        for rid, r in reqs.items():
            all_reqs.setdefault(rid, r)
        ignored.update(p.ignored for p in parsed if p.ignored)
        unknown += sum(e.kind == "unknown" for p in parsed for e in p.events)
        label = f"{meta.agent_type} · {meta.description}" if meta else ""
        print(f"  agente {agent_id:<20} {len(parsed):>6} líneas  {len(reqs):>5} peticiones  "
              f"{_cost(reqs.values()):>12}  {label}")

    print("\nModelo                       input    output   cache_r   cw_5m   cw_1h   coste est.")
    for model, t in sorted(tokens_by_model(all_reqs.values()).items(), key=lambda kv: str(kv[0])):
        cost = _cost(r for r in all_reqs.values() if r.model == model)
        print(f"  {model!s:<24}", *(f"{'?' if v is None else v:>9}" for v in t), f"{cost:>12}")
    print(f"\nunknown: {unknown} · ignorados: {sum(ignored.values())} {dict(ignored)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
