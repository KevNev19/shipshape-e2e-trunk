---
name: shipshape-orchestrate
description: Route Shipshape implementation, investigation, debugging, or review to the opposite-provider orchestrator selected by the actual tool host, then independently verify the result. Use on Claude hosts for Codex workers and on Codex hosts for Claude workers when a bounded stage benefits from delegation; keep tiny routine work in the selected coordinator when another hop costs more than it adds.
---

# Shipshape orchestration

The selected host remains coordinator. It owns intent, authority, planning,
conflicts, independent verification, and the final verdict. A worker executes
one bounded task; it never routes back to the other provider or launches
Shipshape orchestration recursively.

## 1. Select by host

Identify the actual tool host from session context as exactly `codex` or
`claude`. The caller supplies this fact. Never infer it from the model name,
process ancestry, `CODEX_THREAD_ID`, `CLAUDECODE`, or any inherited environment
variable. Preserve the user's selected coordinator and model; `gpt-6-astra` is
one possible Codex coordinator, not a requirement.

Set `SHIPSHAPE_ROOT` to the absolute bundle root discovered from this loaded
skill: from the directory containing this `SKILL.md`, go up two parents. It is
not the target repository or current working directory.

```sh
export SHIPSHAPE_ROOT="/absolute/path/to/the/loaded/shipshape/bundle"
python3 "${SHIPSHAPE_ROOT}/scripts/orchestrator.py" route --host <codex|claude>
```

The helper is read-only. Require exit zero and exactly one JSON object. Its
mapping is deterministic:

- Codex host → `codex-claude-orchestrator` → Claude workers.
- Claude host → `codex-orchestrator` → Codex workers.

Use the returned `plugin`, `skill`, native `entrypoint`, and
`entrypoint_path`. Resolve the installed plugin through the host's skill
registry, obtain the actual absolute path ending in that `entrypoint_path`, and
load the complete upstream `SKILL.md`. Never build a cache path, search from
the target repository, or use Shipshape's root as the dependency root. The
selected upstream skill owns lifecycle and log format.

If the selected plugin or expected version is missing, report the route
object's installation instructions or metadata and stop. Shipshape does not
install dependencies. A change to the other provider requires explicit task
authorization and a completed upstream shutdown/recovery that proves custody
has returned.

## 2. Establish the task boundary

Before delegation:

1. Read the target repository's harness and follow its branch and isolation
   policy. The Shipshape kit itself stays on `main` without branches or
   worktrees; this local policy does not become a requirement for consumers.
2. Capture the full starting commit and exact
   `git status --short --untracked-files=all`. Preserve unrelated dirty work.
3. State one goal, observable acceptance criteria, exact owned paths,
   coordinator host, worker provider, plugin/entrypoint, selection reason,
   exact prompt, and the checks the coordinator will run afterward.
4. Inspect active ownership. Use one writer at a time in each checkout and
   serialize overlapping work. Wait or narrow scope when another
   task owns a path; never clobber concurrent edits.

Already-authorized in-scope work needs no repeated approval. Ask only when a
material choice, authority expansion, or target state is missing.

Record those facts through the selected upstream workflow's normal records.
On the Claude-host route, use the `codex-orchestrator` journal and contract
exactly. On the Codex-host route, retain the route decision and exact
prompt/handoff/evidence with the Codex task record, the coredo registered
identity, and the native Claude transcript; do not impose the other plugin's
journal or invent a parallel schema. In both routes, a handoff is a claim and
repository state plus coordinator-observed checks are evidence.

## 3. Run the selected workflow

### Claude host: Codex workers

Invoke the native Claude entrypoint `/codex-orchestrator:workflow` from the
installed `skills/workflow/SKILL.md`. It owns the full lifecycle: initialize
the run, capture exact prompts/events/handoffs, execute focused tasks, validate,
close, and report.

Apply the target repository's rules while following it:

- The Claude host is coordinator and independently verifies every material
  worker claim.
- Preserve the user's configured Codex model, reasoning, and any profile
  required by the target harness, including on resume. The Shipshape kit's
  own harness uses its existing `shipshape` profile; consumers need no such
  profile. Never create or rewrite user model configuration.
- Follow the target's checkout policy and serialize overlapping work. In the
  Shipshape kit, keep work on `main`; upstream worktree options are unavailable.
- The Codex worker receives a bounded task and the no-reverse-delegation
  invariant. It does not launch Claude, Shipshape, or another orchestration
  loop.

### Codex host: Claude workers

Invoke the Codex-native
`$codex-claude-orchestrator:claude-pty-agents` entrypoint from the installed
`skills/claude-pty-agents/SKILL.md`. The required audited dependency is
`coredo-eu/codex-claude-orchestrator` at
`519ccacda39aafe3ea703cc871d93b141eb8089b`, plugin version
`0.4.0+codex.20260818204818`.

Follow its status, admission, assignment, ownership, handoff, kill-switch,
`/exit`, proven process-group death, rotation, retirement, and recovery rules
exactly. Use the ordinary admitted launch, keep the exact ready identity
(including UUID, root, lease, model, effort, and route), and operate only the
current Codex thread's registered session. While the worker holds edit custody,
the coordinator does not edit the checkout or duplicate its outcome.

Route secondary Claude work by the task, not by keyword counts or universal
benchmark claims:

| Bounded stage | Claude route |
| --- | --- |
| Read-only source discovery, logs, or test triage | Configured `explorer`, `log-analyzer`, `test-triager`, or `scout` on `claude-haiku-4-5-20251001`; effort is fixed. |
| Scoped implementation | `claude-sonnet-5` parent at `high`; use the `implementer` role at `high` when isolated edit custody adds value. |
| Difficult multi-step diagnosis | `claude-sonnet-5` parent at `high`, with the read-only `debugger` role at `xhigh`. |
| Material judgment or independent review | `claude-opus-5` parent at `high` with an explicit audited route reason, or the `reviewer` at `medium` / `security-reviewer` at `xhigh`. |

Assess complexity, ambiguity, blast radius and risk, separability, verification
strength, cost, and latency. Tiny routine work may stay in the Codex
coordinator. Fable is outside the default path; use an upstream long-horizon
route only for demonstrated need, confirmed access, and explicit sole custody.
An unavailable route returns to the coordinator for a new decision, never a
silent model or provider fallback.

#### PTY prompt transport

Raw multiline `write_stdin` dropped the first prompt sections in live testing.
Bracketed paste preserved a three-line probe byte-for-byte in the native Claude
transcript. After the upstream assignment gate, send the exact prompt in one
write without a carriage return:

```javascript
const pastedPrompt = "\x1b[200~" + exactPrompt + "\x1b[201~";
await tools.write_stdin({ session_id, chars: pastedPrompt });
```

Verify the complete received prompt in the transcript or input echo, from its
first line through its final handoff instruction. Only then send the carriage
return in a separate call:

```javascript
await tools.write_stdin({ session_id, chars: "\r" });
```

Reject a truncated or unverified prompt and any result derived from it. Repeat
the upstream kill-switch and retirement preflight before every send or poll.

The worker prompt keeps the upstream contract headings verbatim and ordered:
`Outcome`, `Done when`, `Boundaries`, `Authoritative context`, `Non-goals`,
`Known evidence`, and `Required handoff`. Include the exact owned paths,
selected role/reason, no-reverse-delegation invariant, and required terminal
marker inside that contract.

## 4. Verify and close

After custody returns, preserve the exact handoff and runtime identity. Compare
the repository with the baseline, inspect every owned change, confirm unrelated
dirty work survived, and independently execute the acceptance checks. Keep
failed observations and passing reruns; a stronger worker model never makes
acceptance automatic.

Run the target repository's required checks from its harness or SDLC config
when project-level acceptance is due. For the Shipshape kit itself, these are:

```sh
python3 -m pytest tests/ -q
ruff check . && ruff format --check .
```

Report unrelated failures honestly without changing them. Commit, push,
install, publish, or deploy only when the active user request expressly
authorizes that action.

Return the handoff with these exact headings:

```markdown
## Status

## Summary

## Files Changed

## Claims / Findings

## Commands Reported

## Caveats / Blockers
```
