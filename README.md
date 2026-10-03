# traza

A local, read-only, live dashboard for Claude Code. It reads the session logs Claude Code already
writes to `~/.claude/projects` and shows every agent and subagent, what each one is doing right
now, and how many tokens each one used, with their value at API prices: what those tokens would
cost on Anthropic's API, not what you paid.

![A subagent is launched and moves up the agent tree while it works; the Flame view then shows which agents account for most of the value at API prices](docs/demo.gif)

*The GIF uses synthetic sessions made by [`tools/make_demo_data.py`](tools/make_demo_data.py),
not real ones.*

**Status:** v0.0.1, early. Developed and tested on Windows 11 with logs from Claude Code
2.1.226–2.1.288.

## Install

You need Python 3.11+ and a machine where Claude Code has already run (traza reads its logs;
with none, the panel is empty until a session starts).

```sh
pipx install git+https://github.com/LanderIglesias/traza
# or: uv tool install git+https://github.com/LanderIglesias/traza
# or, without pipx/uv: pip install git+https://github.com/LanderIglesias/traza
traza serve
```

`traza serve` opens `http://127.0.0.1:7420` in your browser. Two dependencies (FastAPI, uvicorn),
no account, no API key, and nothing leaves your machine. The first start builds a local cache
(`~/.traza/traza.db`): about 3 s for 110,000 log lines on the author's machine, longer on a cold
disk or with an antivirus scanning. After that it only reads new lines.

## Usage

```sh
traza serve                    # the dashboard; Ctrl+C to stop
traza serve --port 7421        # if 7420 is taken (traza says so and exits)
traza serve --no-open          # don't open the browser
traza scan                     # build or refresh the cache and print what it holds
traza report SESSION.jsonl     # tokens and value per model of one session, straight from the log
```

`--root` and `--db` point traza at other log and cache locations. To remove everything traza
created, delete `~/.traza`.

## What it does

- **Agent tree, live.** Every session with its agents and subagents, nested as they were
  launched, each with its state (running a tool, thinking, idle, done), updated as Claude Code
  writes. A Timeline tab shows when each agent was working.
- **Value per agent.** Tokens and their value at API prices per request, agent and session, for
  each agent alone and including the subagents it launched. A Flame tab (a flame chart: width =
  value) makes forty subagents of the same type distinguishable at a glance.
- **Per-agent detail ("judgement view").** For any agent: the task it was given, its tool calls
  turn by turn, and what it returned to its parent, so you can judge whether it did its job.
  Long text is read from the log when you open it; the cache doesn't copy it.
- **Signals.** Tool errors, calls blocked by permissions or hooks, API errors, loops (the same
  call three times in a row), retries, tools that seem stuck, and context compactions, each
  linked to the turn where it happened.

A **health bar** at the bottom says how much of the logs traza understood: lines it didn't
recognise, lines it skips on purpose, and requests whose token counts look wrong.

What it showed on the author's machine: in the two sessions with about 40 subagents (42 and 39),
the main thread accounted for 90% and 96% of the value. The many subagents were cheap; carrying
the main thread's long context was the expensive part. In a session where a task was split into
24 short subagents, the subagents took 57%.

## What it does not do

- It does not think: no model calls, no summaries, no "AI insights". Everything shown is read
  from the logs or computed from them.
- It does not launch, stop or steer agents. It never writes to `~/.claude`.
- It does not show the model's reasoning: Claude Code stores thinking blocks with empty text.
- It does not keep history. It mirrors the logs on disk, and Claude Code deletes them after
  30 days by default, so the panel covers a rolling 30-day window. To keep more, raise
  `cleanupPeriodDays` in Claude Code's settings. traza won't archive logs itself; that would be
  a different product.

## What the figures mean

**Value at API prices is not money you spent.** It is what the tokens would cost at Anthropic's
public API prices ([`traza/prices.toml`](traza/prices.toml)). With a subscription you don't pay
per token, but it still shows where the volume went.

**How the token counts are checked.** Now and then Claude Code writes a `cost-state` line: its
own running per-model token total for the current process. A test
([`tests/test_oracle.py`](tests/test_oracle.py), run locally against real logs) compares traza's
count with it. Four sessions on the author's machine have one:

- In two, the automated check matches **to the token and to the micro-dollar** (e.g. 23,334
  output tokens and $2.012743 on both sides).
- In a third, Claude Code's own counter restarted mid-session, after an 18-hour pause, without
  changing its start time, so the automated comparison disagrees. Counted by hand from the
  restart, it matches exactly.
- The fourth is a copied session: its log carries history the process never spent, so it is
  reported, not compared.

These lines are rare, so this is a spot check of accuracy, not a regression suite. It also
cannot catch a mistake that traza and the check would both make; separate tests cover each
counting rule with data where a wrong rule gives a different answer.

Other rules behind the numbers:

- **`?` means unknown, never zero.** A model missing from the price table, or a request without
  token counts, shows `?`. A total with some unknown parts shows `$X+`.
- **Internal cost is "at least".** Claude Code makes calls of its own (titles, summaries, usually
  on a small model) and does not log them. traza can measure them from `cost-state` only for
  models the session never used in a logged request; calls to a model it also used can't be
  separated. So the health bar says "internal cost measured: at least $0.09 in 3 sessions", and
  says nothing when there is nothing to measure.
- **Implausible token counts are flagged, not fixed.** Sometimes Claude Code never writes the
  final line of a response with its full token count (466 requests on the author's machine, e.g.
  7 output tokens for 10,254 characters of text). traza counts these in the health bar; it can't
  recover the real number.

## Known limits

- **Subagents with no recorded end.** For 3 of 131 subagents on the author's machine, Claude
  Code never wrote an end, so they stay "idle" instead of "done" (a tooltip says why). The log
  doesn't contain it; it is not a panel bug.
- **Logs rewritten while traza runs.** Detecting a log that is truncated or replaced during a
  live session is covered by tests, but was not reproduced against real logs, to avoid risking
  them.
- **Other user accounts on the same machine.** The server listens only on `127.0.0.1` and
  rejects requests whose `Host` or `Origin` isn't its own (protection against DNS rebinding),
  but another user account on the same computer can read the dashboard while it runs. On a
  single-user machine that crosses no boundary. One related browser scenario is not yet
  validated (notes in [`docs/design.md`](docs/design.md) §9).

## How it is built

FastAPI + Server-Sent Events + SQLite (WAL), and plain HTML, CSS and JavaScript: no build step,
no frontend framework, no CDN. A watcher reads new log lines every 0.5 s into a disposable cache:
delete it and it is rebuilt from the logs. Log text is always rendered with `textContent`, never
as HTML. Design decisions and the evidence behind them, in Spanish:
[`docs/design.md`](docs/design.md), [`docs/findings.md`](docs/findings.md).

```sh
pip install -e ".[dev]"
pytest    # the Flame layout tests need Node (skipped without it); test_oracle reads ~/.claude
```

## License

MIT
