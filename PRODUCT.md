# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary:** the author, daily, while working with Claude Code (mostly from the VS Code
  extension) and launching many subagents. Job: see live which agents and subagents are running,
  what each one is doing, what it costs, and judge whether its output is good.
- **Secondary:** a technical recruiter or portfolio reviewer. Job: understand what the tool is
  in 30 seconds (README + GIF) and install it with one command.

## Product Purpose

A local, read-only web panel that opens Claude Code "like a glass box": the live hierarchy of
agents and subagents and their state, tokens and **estimated** cost per request, agent, session
and model, and a timeline of every tool call and result. Success = the author can tell, while a
session runs, which subagent is burning money and whether its work is any good.

## Positioning

Not just "how much did I spend": a tool to **judge the quality of each subagent's work** (its
task and what it returned, side by side, plus deterministic signals), with **demonstrated
accuracy** — token counts match Claude Code's own `cost-state` exactly, to the token and the
micro-dollar — and honest about what it does not know: `?` instead of invented numbers.

## Operating Context

- Runs next to Claude Code on the same machine; reads `~/.claude/projects/**/*.jsonl`
  read-only; bound to `127.0.0.1`. Nothing leaves the machine, no paid services.
- Open in a browser tab beside the editor during long sessions; glanced at, not stared at.
- History is whatever Claude Code keeps on disk (30-day rolling window by default).

## Capabilities and Constraints

- Backend: Python (FastAPI, SSE, SQLite as a disposable cache). Frontend: plain HTML/CSS/JS
  modules, no framework, no build step.
- Cost is always labelled **estimated** (public prices applied to past sessions; not the bill).
- Unknown values show as `?`, never as 0 or a plausible guess.
- Thinking text is empty on disk: the panel shows actions and results, not reasoning.
- Content from transcripts is untrusted: always rendered as text (`textContent`), never HTML.
- UI language: **English**.
- Undecided before F3: how a session with inherited requests shows its cost (docs/design.md §7,
  option (a) recommended).

## Brand Commitments

- Name: **traza** (lowercase).
- Binding visual reference supplied by the author (2026-09-30): a light fintech-style
  dashboard — very light grey canvas, white cards with large corner radii, black pill
  navigation, slim left icon rail, one coral/red gradient accent card and one black feature
  card, sparklines, a table with coloured status pills (failed red / successful green / pending
  orange), geometric sans type, big bold numbers. The image itself is third-party and is not
  stored in the repo.

## Evidence on Hand

- Oracle result: exact token and dollar match on two real sessions (docs/findings.md §4).
- Real-data runs: ~90k lines, 0 unknown, full build ~1.7 s (docs/findings.md §F2).
- No users, testimonials or benchmarks beyond these; do not invent any.

## Product Principles

1. Truth over polish: a number shown is a number verified, or it is `?`.
2. Glanceable live state first; detail on demand.
3. Judge the work, not just the bill.
4. Local and private by construction.
