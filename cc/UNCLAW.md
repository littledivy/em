# unclaw workstream

Manual-only. No auto-spawning, no scheduled ticks, no fallback picker. Workers spawn ONLY when the operator explicitly asks ("spawn an unclaw worker for X").

## Repo + auth

- Upstream: `denoland/unclaw` (INTERNAL visibility, default branch `main`, **forking disabled**).
- Auth: **`littledivy`** account. divybot is in the denoland org but only has READ on unclaw and can't fork. littledivy has MAINTAIN — can push topic branches directly.
- Local clone: `~/src/unclaw`. `origin` remote uses littledivy token.
- Per-task worktrees: `~/src/unclaw-wt/<slug>`, branch `claude/<slug>`.
- Push target: direct push to `denoland/unclaw:claude/<slug>`, PR from `claude/<slug>` → `main` (same repo).
- Commits authored by `littledivy` (the only account that can push). No Claude co-author trailer (matches node-compat).
- Worker tmux sessions still spawn with `GH_TOKEN=<littledivy>` exported via unclaw.sh, so `gh issue view`, `gh pr view` etc. all work as littledivy inside the worker.
- After the worker prints `<<NODE_BOT_DONE>>` and tick.py opens the PR, the same worker session stays alive in **monitoring** mode. It launches `gh pr checks --watch` in background and uses the **Monitor tool** (event-driven, not sleep-polling) to react to CI events; on failure it fixes + pushes via `git push origin HEAD`; on green it prints `<<NODE_BOT_DONE>> ci passed` and exits.
- On reviewer feedback after CI green: tick.py's review-poll auto-respawns the worker with claude --continue in the unclaw worktree + littledivy auth + a checklist. Same autonomous loop as node-compat.
- No cloud `/autofix-pr` — needs Claude GitHub App installed on the repo (operator opted out).

## How to invoke (operator-triggered)

When operator says e.g. "spawn an unclaw worker to add validation for X":

```bash
bash /Users/divy/cc/unclaw.sh <slug> "<task description>"
```

`<slug>` is a short kebab-case identifier used for branch + worktree + tmux session names (e.g. `add-x-validation`).

The script:
1. Switches gh to `littledivy` for the session's lifetime (no global change).
2. Clones `denoland/unclaw` to `~/src/unclaw` if missing.
3. Adds a worktree on `claude/<slug>` from `origin/main`.
4. Trusts the worktree, spawns `claude` TUI in a `unc-<slug>` tmux session on the shared socket.
5. Sends `/remote-control` so the session is visible at claude.ai/code.
6. Pastes the prompt: `prompt-unclaw.md` template + the task description the operator gave.

The same `<<NODE_BOT_DONE>> <PR title>` / `<<NODE_BOT_ESCALATE>> <reason>` sentinel convention applies — workers signal completion the same way as node-compat. (Detector regex in tick.py works across both, but tick.py doesn't auto-poll unclaw tasks; operator drives.)

## Operator levers

| Want to | Do |
|---|---|
| Spawn an unclaw worker | `bash /Users/divy/cc/unclaw.sh <slug> "<task>"` |
| Peek the worker | `tmux -S <socket> attach -t unc-<slug>` (or claude.ai/code) |
| Send guidance | `tmux -S <socket> send-keys -t unc-<slug>:0.0 -l "<msg>"; tmux ... send-keys ... Enter` (no inbox-via-tick since tick.py doesn't process unclaw) |
| Open the PR yourself | When worker prints `<<NODE_BOT_DONE>>`, run the equivalent of post_worker manually (commit, `git push origin claude/<slug>`, `gh pr create --repo denoland/unclaw --head littledivy:claude/<slug>` etc) — OR adapt unclaw.sh later if this gets repetitive. |
| Stop a worker | `tmux -S <socket> kill-session -t unc-<slug>` |

## Why no auto-spawn for unclaw

Operator: "I don't want auto spawn workers for it, only when I tell you when I need." Each unclaw task is bespoke (operator-defined description), unlike node-compat where the task pool is a public failing-test list. No queue file, no picker, no daily cap entry — keep state minimal until the workstream proves it needs more structure.

## TBDs (ask operator before first real run)

- Push target: direct to `denoland/unclaw` or via `littledivy/unclaw` fork?
- Co-author trailer? (node-compat has none per operator preference.)
- Any "always include" context (architecture, team conventions) to prepend to every prompt?
