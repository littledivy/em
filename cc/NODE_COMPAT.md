# node-compat workstream

The first (and currently only) workstream for the deno-bot orchestrator. Goal: land minimal fixes in `denoland/deno` that flip Node-compat tests from failing to passing.

When new workstreams are added, give each its own `<NAME>.md` and link it from `CLAUDE.md`. This file should stay node-compat-specific.

## Mission

Pick a failing Node-compat test, spawn a worker that fixes the polyfill so that test passes, open a PR, shepherd it through review and CI to merge. Repeat. Operator-paced (caps: 3 concurrent workers, 10 PRs open at any time — merged/closed PRs free a slot).

## The fleet

- **Orchestrator** = me, the Claude session the operator is talking to. I pick tasks, monitor workers, push guidance, fix tick.py / prompt.md when something breaks, and report status to the operator.
- **Worker** = a separate `claude` TUI process running in a detached tmux session per task. Each worker lives in its own git worktree (`~/src/deno-wt/<task>`) on its own branch (`claude/<task>`). Workers do the actual coding.
- **tick.py** = the glue. One invocation does ONE pass: deliver inbox messages, poll running workers, poll review PRs, spawn one fresh task if a slot is free. Idempotent — safe to call any time.
- **Operator** = the human. Pokes me with "tick" or asks status. Decides direction on ambiguous calls.

## State machine

```
                             ┌── duplicate of upstream PR ──→ abandoned
                             │
   queue/picker → running ───┼── <<NODE_BOT_DONE>> ─→ post_worker ──→ monitoring ──→ review ──→ merged
                             │                                        │
                             ├── <<NODE_BOT_ESCALATE>> → abandoned    └─ (worker watches CI events via Monitor tool)
                             ├── no diff / sprawl ──→ abandoned
                             └── session died / idle ──→ failed
```

**Statuses:**
- `running` — worker is doing the initial fix OR re-engaged on PR feedback.
- `monitoring` — PR open; worker stays alive and uses the **Monitor tool** to watch `gh pr checks --watch` events (event-driven, no sleep loops). On failure it fixes + pushes; on green it prints `<<NODE_BOT_DONE>> ci passed` and exits.
- `review` — CI green or worker exited; orchestrator's review-poll watches for human-review activity and respawns worker on new comments/reviews.
- `merged` — landed; worktree deleted.
- `abandoned` / `failed` — terminal; worktree source kept, `target/` wiped.

Slot cap (3 concurrent) counts `running + monitoring`.

**No cloud `/autofix-pr`** — it 404s on cross-repo (fork→upstream) PRs ([#223](https://github.com/anthropics/claude-code-action/issues/223), [#821](https://github.com/anthropics/claude-code-action/issues/821)) AND requires the Claude GitHub App installed on the target repo (operator opted out).

Worktree is preserved on every terminal state EXCEPT `merged` (which deletes it). Cargo `target/` is wiped on every non-success cleanup to reclaim ~10–15GB.

## Picking tasks (when queue is empty)

Don't let the auto-picker scan alphabetically — most early-alphabet failures are `--expose-internals` traps we can't fix. Pick with judgment:

```bash
# 1. Pull failing parallel/* tests from the public viewer
curl -fsSL https://node-test-viewer.deno.dev/results/latest/darwin.json \
  | jq -r '.results | to_entries[] | select(.key|startswith("parallel/")) | select(.value[0]==false) | .key | sub("^parallel/";"") | sub("\\.js$";"")' \
  > /tmp/failing.txt

# 2. Drop categories that almost always need Rust core
grep -vE 'tls|cluster|spawn|fork|child[-_]process|crypto|http2|inspector|debugger|repl|wasm|v8|napi|worker[-_]thread|trace|perf_hook|dgram|dns|zlib|snapshot|heap|sqlite|sea|preload|loader|esm|cli-' /tmp/failing.txt > /tmp/cand.txt

# 3. For each candidate, drop --expose-internals / Node CLI flags / huge files
DIR=$HOME/src/deno/tests/node_compat/runner/suite/test/parallel
while read t; do
  f=$DIR/$t.js; [ -f $f ] || continue
  head -3 $f | grep -qE -- '--expose-internals|--disable-proto|--pending-deprecation|--require|--input-type|--build-snapshot|--heap-prof|internal/' && continue
  L=$(wc -l < $f); [ $L -lt 80 ] && [ $L -gt 5 ] && printf '%3d %s\n' $L $t
done < /tmp/cand.txt | sort -n | head -30
```

Then **read** the top 10 candidates (their actual test code is short) and **reason** about each:
- Pure-JS polyfill territory → good (URL, util, console, process arg-validation, timers/promises, querystring, string_decoder, punycode, path, os, assert).
- Needs Node internals (`process.binding()`, `internal/...`) → skip; we can't expose them.
- Needs Node-only CLI flag → skip; that's Rust.
- Touches network/crypto/wasm/V8 → skip; almost always Rust ops.

Show the operator your picks with a one-line reason each, then queue them:

```bash
printf '%s\n' test-foo test-bar test-baz >> ~/.deno-bot/queue.txt
```

`tick.py` drains queue before falling back to the auto-picker.

## Monitoring workers

Each tick:
1. Run `python3 /Users/divy/cc/tick.py`.
2. Read its log lines (active / DONE / ESCALATE / fed back / spawned / waiting CI).
3. For anything looking stuck (long idle, weird output), peek the tmux pane:
   ```
   tmux -S /var/folders/pz/*/T*/claude-tmux-sockets/deno-bot.sock capture-pane -p -t dn-<task>:0.0 -S -50
   ```
4. If they're spinning on something fixable (toolchain trap, wrong direction, duplicate PR, etc), drop guidance into `~/.deno-bot/inbox/<task>.txt`. Tick delivers it next pass.
5. Report status concisely to operator. Schedule next tick via `ScheduleWakeup` (~270s keeps prompt cache warm) when working autonomously.

**NEVER write the literal sentinel strings `<<NODE_BOT_DONE>>` or `<<NODE_BOT_ESCALATE>>` in inbox messages or anywhere they'd land in a worker's tmux pane.** The detector matches assistant-bullet (`⏺`) prefix only, so user-pasted text shouldn't trip it — but defensive habit.

## How workers complete

Workers print one of two sentinel lines (with the `⏺` claude bullet that the TUI adds automatically when the assistant prints a final message):

- `<<NODE_BOT_DONE>> <full PR title>` — worker chose the title, conventional-commit form. Orchestrator commits + pushes to bot fork + opens PR upstream with that exact title.
- `<<NODE_BOT_ESCALATE>> <reason>` — worker gives up. Task marked abandoned, session killed, target/ wiped, source kept.

Soft scope target: ~5 files / ~200 LOC. Hard sprawl auto-abandon at >10 files or >400 LOC (post_worker checks before opening PR).

## Watching the PR

**Two layers, split by what's visible to whom:**

1. **Real-time CI watch (worker-driven, event-based)** — when `post_worker` opens a PR, the worker session stays alive. It launches `gh pr checks <PR> --watch` in the background (`run_in_background=true`) and attaches the **Monitor tool** to that process. Monitor surfaces each new line as an event; worker only spends tokens when there's real output (no sleep-polling, no 10s ticks). On any failure: fetch failed log, fix, commit, `git push bot HEAD`, relaunch watch. On all green: print `<<NODE_BOT_DONE>> ci passed`. This is the `monitoring` status.

2. **Human-review polling (orchestrator-driven)** — once CI is green and the worker exits, task moves to `review`. The orchestrator's review-poll loop hashes (state, statusCheckRollup, comments, reviews, inline review threads) — bots filtered. On change:
   - `MERGED` → mark merged, delete worktree.
   - `CLOSED` → mark abandoned, keep worktree.
   - Otherwise → respawn worker via `claude --continue`, paste a checklist, worker fixes + pushes.

## Operator levers

| Want to | Do |
|---|---|
| Force a specific test next | `echo "test-foo-bar" >> ~/.deno-bot/queue.txt` |
| Send guidance to a running worker | `echo "<msg>" > ~/.deno-bot/inbox/<task>.txt` (delivered next tick) |
| Halt all spawns | `touch ~/.deno-bot/halt`. Resume: `rm ~/.deno-bot/halt` |
| Stop everything right now | halt + kill tmux sessions: `tmux -S <socket> kill-session -t dn-<task>` per task |
| See live worker | `tmux -S <socket> attach -t dn-<task>` (or claude.ai/code if `/remote-control` was sent) |
| Inspect history | `sqlite3 ~/.deno-bot/tasks.db "SELECT id,status,attempts,last_error,pr_url FROM tasks ORDER BY updated_at DESC;"` |

## Key files

| Path | Purpose |
|---|---|
| `/Users/divy/cc/tick.py` | The driver. Read top-to-bottom to understand the orchestrator. |
| `/Users/divy/cc/prompt.md` | Per-worker prompt template. Substitutions: `<NAME>`, `<file>`. |
| `/Users/divy/cc/trust.sh` | Pre-marks a worktree as trusted in `~/.claude.json` so claude skips the dialog. |
| `/Users/divy/cc/CLAUDE.md` | Project instructions (auto-loaded by claude). Points to this file. |
| `~/.deno-bot/tasks.db` | Sqlite state. Schema in `tick.py`. |
| `~/.deno-bot/queue.txt` | Forced-task queue, one name per line. Drained top-down. |
| `~/.deno-bot/inbox/<task>.txt` | One-shot guidance for a running worker. Deleted after delivery. |
| `~/.deno-bot/halt` | Empty file. Presence stops new spawns and exits tick early. |
| `~/.deno-bot/logs/launcher.{out,err}` | Historical tick output (if launcher.sh is wrapping ticks). |
| `~/src/deno` | Base deno repo (shared `.git`). |
| `~/src/deno-wt/<task>` | Per-worker worktree. |

## Troubleshooting

- **`tmux: command not found` / `jq: command not found`** — tick.py exports `PATH=/opt/homebrew/bin:$PATH`. Verify both are installed there.
- **Worker spawns but prompt never submits** — paste-buffer needs `sleep 1` before `Enter` (TUI processes paste asynchronously). tick.py has this.
- **`claude remote-control` mode shows daemon view, no input** — that's expected; it's a hub. Use plain `claude` TUI + `/remote-control` slash command for visibility on claude.ai/code.
- **PR title is garbage** — usually the false-DONE detector grabbed unrelated text. Detector requires `⏺ <<NODE_BOT_DONE>> ` (claude assistant bullet prefix). If it false-positives, check what's in the worker pane.
- **Worker keeps fighting toolchain (libiconv/dsymutil/cmake)** — they're using bare `cargo`. Send inbox: "use `nix develop -c cargo ...`". prompt.md already says this.
- **Worktree exists conflict on respawn** — pre-existing worktree from prior failed run. `git -C ~/src/deno worktree remove --force <path>` then re-tick.

## Constraints to keep workers from sprawling

- No Rust core edits. Worker must `<<NODE_BOT_ESCALATE>>` if a fix needs Rust (`deno_core`, `ext/node` Rust, V8). Operator handles those manually.
- ESCALATE if test depends on unimplemented Node feature (e.g. `process.binding('uv')`, `--pending-deprecation`, `internal/...` modules).
- Soft target ~5 files / ~200 LOC; auto-abandon at 10 files / 400 LOC.
- Worktree-only; never touch the base `~/src/deno` checkout.
