"""Test oráculo contra cost-state (design.md §8). Solo en local: lee ~/.claude/projects, no
commitea datos. Correr con `pytest -s tests/test_oracle.py` para ver el informe."""
import json
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from traza.parser import dedupe_requests, tokens_by_model
from traza.pricing import request_cost
from traza.report import parse_file

PROJECTS = Path.home() / ".claude" / "projects"
HEALTHY = {"36b96010", "598796c2"}  # sesiones sanas que deben cuadrar exacto (findings.md §4)


def ms(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000


def oracle_checks():
    """Por cada línea cost-state: (sesión, modelo, cuadra_tokens, cuadra_dólares, detalle)."""
    mains = sorted(PROJECTS.glob("*/*.jsonl"))
    owners = Counter()  # en cuántos ficheros aparece cada requestId → detecta copias
    parsed_by_file = {}
    for f in mains:
        parsed_by_file[f] = parse_file(f)
        owners.update({p.request.request_id for p in parsed_by_file[f] if p.request})
    out = []
    for f in mains:
        parsed = parsed_by_file[f]
        with open(f, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for i, raw in enumerate(lines):
            if '"cost-state"' not in raw:
                continue
            cs = json.loads(raw)
            window = dedupe_requests(
                p.request for p in parsed[:i]
                if p.request and p.request.timestamp and ms(p.request.timestamp) >= cs["startTime"])
            is_copy = any(owners[rid] > 1 for rid in window)
            # Aviso, no regla (un solo caso visto, 7bc000bb): varios cost-state del mismo proceso
            # (mismo startTime) pueden venir de un contador reiniciado tras una pausa larga sin
            # cambiar startTime; entonces la ventana [startTime, línea] cuenta de más.
            same_process = sum('"cost-state"' in x and json.loads(x).get("startTime") == cs["startTime"]
                               for x in lines[:i + 1])
            if same_process > 1:
                warnings.warn(f"{f.stem[:8]}: {same_process} cost-state con el mismo startTime; "
                              "el oráculo puede no ser fiable en esta sesión")
            # Solo se leen tokens del fichero principal: con subagentes la comparación no es fiable
            # (no se sabe si cost-state suma su gasto). Se avisa en vez de dar un verde que miente.
            has_subagents = (f.parent / f.stem / "subagents").is_dir()
            ours = tokens_by_model(window.values())
            for model, mu in cs["modelUsage"].items():
                t = ours.get(model)
                if t is None:
                    out.append((f.stem[:8], model, None, None, "solo en cost-state: interno"))
                    continue
                tok_ok = (t.input, t.output, t.cache_read, t.cache_write_5m + t.cache_write_1h) == (
                    mu["inputTokens"], mu["outputTokens"], mu["cacheReadInputTokens"],
                    mu["cacheCreationInputTokens"])
                cost = sum(request_cost(r) or 0 for r in window.values() if r.model == model)
                usd_ok = abs(cost - mu["costUSD"]) < 1e-6
                if has_subagents:
                    tok_ok = usd_ok = None
                    warnings.warn(f"{f.stem[:8]}: cost-state en sesión con subagentes, no comparable")
                note = "copia (known issue)" if is_copy else ""
                out.append((f.stem[:8], model, tok_ok, usd_ok,
                            f"out {t.output} vs {mu['outputTokens']} · ${cost:.6f} vs "
                            f"${mu['costUSD']:.6f} {note}"))
    return out


@pytest.mark.skipif(not PROJECTS.exists(), reason="sin ~/.claude/projects en esta máquina")
def test_oraculo_cost_state():
    checks = oracle_checks()
    for c in checks:
        print(*c)
    present = HEALTHY & {c[0] for c in checks}
    for s in sorted(HEALTHY - present):
        print(s, "ausente (limpieza de 30 días o nunca estuvo): no se exige")
    if not present:
        pytest.skip("ninguna sesión sana sigue en disco")
    failed = [c for c in checks if c[0] in present and c[2] is not None and not (c[2] and c[3])]
    unreliable = sorted({c[0] for c in checks if c[0] in present and c[2] is None
                         and "interno" not in c[4]})
    assert not unreliable, f"sesiones sanas ahora con subagentes, revisar el oráculo: {unreliable}"
    assert not failed
