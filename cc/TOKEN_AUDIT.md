# Token audit — deno-bot fleet

Sampled ~12 worker sessions plus the orchestrator at
`/Users/divy/.claude/projects/`. 118 worker jsonls, 285 detected respawns,
101 MB of worker session bytes plus 15 MB orchestrator. Token figures use
bytes/4 as a rough estimate.

## TL;DR — biggest token sinks

| Rank | Sink | Est. tokens (so far) | Est. tokens (going forward, per cycle) |
|---:|---|---:|---:|
| 1 | Orchestrator session never rotates (16 MB, 106 wakeups, 4 days) | ~210 M | grows quadratically; another 200 M+ if untouched |
| 2 | Worker resume replay across many respawns (median 62 min gap = cache miss) | ~110 M | ~5–8 M per long-lived task |
| 3 | Same files re-Read 30–80× per worker (slice reads, not full) | ~3–5 M | ~500 K per long task |
| 4 | Respawn nudge boilerplate pasted verbatim every cycle | ~110 K | ~1.5 K × N respawns |
| 5 | `task_reminder` system-reminder spam (1,689 pings fleet-wide, mostly empty) | ~140 K | grows with assistant turns |
| 6 | Initial-spawn fixed context (deno CLAUDE.md + prompt.md per fresh worker) | ~500 K | ~4 K per spawn |

The first two dominate by ~3 orders of magnitude. Everything else is rounding error
unless you're doing a deeper rewrite.

---

## 1. Orchestrator session is a 4-day write-only log — rotate it. **Biggest single win.**

**Evidence.** `~/.claude/projects/-Users-divy-cc/8f8b3c58-3b7f-4144-9167-8d061f223915.jsonl`:
- 16,083,473 bytes (~4 M tokens at end), 10,658 lines
- First ts `2026-04-25T07:37:39Z`, last ts `2026-04-29T07:11:01Z` — single session, 4 days
- 106 `ScheduleWakeup` calls (loop-mode wakeups), 432 last-prompt entries
- 1,624,330 bytes of tool-result content (~406 K tokens) — one fifth of the file
- ScheduleWakeup intervals: 24× 270 s (cache-warm), 17× 1200 s, 6× 1500 s, **59× 3600 s** (cache-cold)

**Why it costs.** Each wakeup is effectively `claude --resume` on this jsonl. With
prompt cache TTL of 5 min, the 59 wakeups at 3600 s and 23 at 1200/1500 s all
hit cold cache and re-tokenize the cumulative file. Half-final-size approximation
across 106 wakeups: **~813 MB ≈ 213 M tokens reloaded** so far. Going forward, the
file keeps growing — costs scale O(N²) in wakeup count.

**Top tool-result eaters in the orchestrator session** (
`(LARGE_FILE=/Users/divy/.claude/projects/-Users-divy-cc/8f8b3c58-3b7f-4144-9167-8d061f223915.jsonl)`):
- 438× `cat /private/tmp/claude-501/.../tool-results/...` → 176 KB total — the orchestrator keeps
  pulling its own file-history-snapshot results back into context
- 54× `SOCK=…/deno-bot.sock; tmux capture-pane …` blocks → 133 KB
- 25× `tail -300 /tmp/tick.log; ps -ef | grep tick.py` → 47 KB
- 31× `bash /Users/divy/cc/tick.sh 2>&1; … sqlite3 …` → 45 KB
- 21× `python3 /Users/divy/cc/tick.py 2>&1 | tail -25` → 32 KB
- 76× `Read /Users/divy/cc/tick.py` (this file is 66 KB; reads are bounded slices but still 50+ K tokens cumulative)

**Recommendation — start a fresh orchestrator session daily.**
`CLAUDE.md` plus a one-paragraph "current state" handoff (open PR list, halt status,
in-flight tasks) is all the orchestrator actually needs to resume work. The 4-day
log of `tail /tmp/tick.log` outputs and tmux pane captures is dead weight.
- Have the orchestrator `/clear` (or operator starts a new session) every ~24 h.
  At current trajectory that caps a session at ~4 MB instead of growing past 16.
- Estimated savings going forward: **~80% of orchestrator wakeup-replay cost**, on
  the order of 100 M tokens/month at current pace.

**Cheaper alternative if rotation is too disruptive:** stop using ScheduleWakeup
at 3600 s. The 59 hour-long sleeps are the worst-of-both: cache-cold AND
cumulative-context replay. Use 270 s for active monitoring, then `/clear` for
truly idle periods. The file `~/cc/CLAUDE.md` already says "~270s keeps prompt
cache warm" — the orchestrator just isn't following it on its longer waits.

---

## 2. Worker `--resume` cost: long-tail tasks dominate

**Evidence.** Per-worker resume reload byte counts (cumulative file size at each
respawn point, summed). Top spenders:

| Task | File MB | Respawns | Median gap (min) | Est. resume tokens |
|---|---:|---:|---:|---:|
| test-http2-padding-aligned | 6.4 | 11 | 62 | ~9 M |
| test-https-agent-session-reuse | 2.5 | 28 | <30 | ~9 M |
| test-vm-module-after-evaluate | 2.4 | 26 | varies | ~7 M |
| test-cluster-shared-handle-bind-error | 3.3 | 18 | varies | ~7 M |
| test-http2-session-settings | 2.9 | 15 | varies | ~5 M |
| test-cwd-enoent-repl | 3.1 | 5 | varies | ~3 M |

Fleet-wide: **285 respawns, ~110 M tokens reloaded.** Median respawn gap is 61.9 min
across 231 inter-respawn intervals. Only **16 % land inside the 5-min cache window**;
54 % are >60 min apart (full cache miss).

**Two recommendations, independent:**

### 2a. Reduce respawn frequency on already-open PRs.

`tick.py` re-engages a worker whenever the PR's content hash changes (failing
checks count, new comments, new reviews, inline comments). Workers with high
attempt counts (`27, 26, 19, 15, 14, 12, 11`) iterate over many tiny CI
fluctuations.

- Coalesce respawns within a window. e.g. don't respawn the same PR more than
  once per 30 min. The 28-respawn `test-https-agent-session-reuse` worker
  almost certainly woke up multiple times in short succession for the same
  underlying review thread.
- For `monitoring` state (CI-watching), keep using the in-session `Monitor`
  tool rather than killing/respawning — that keeps cache warm and avoids
  reloading the whole jsonl. The current code path (line 1195
  `respawn_worker_for_feedback`) calls `cli.resume(sid, task)` even when the
  worker tmux session is alive (line 1229 — only if not present). Good. But
  `paste_update_to_live_worker` (line 1104) appears to do the cheap path
  already; verify the orchestrator preferentially takes that branch.

### 2b. Compact worker history at the "monitoring" boundary.

Once a PR is open, the initial `cargo test` debug spelunking (the bulk of the
session) is no longer relevant; only the diff and the test name matter for
review feedback. A `claude /compact` (or terminate + spawn fresh with PR-only
context) before transitioning to `monitoring` would cut the resume size by
50–80%.

**Estimated savings:** halving worker resume bytes saves ~55 M tokens going
forward at current task volume (~30 tasks/week × 10 respawns avg).

---

## 3. Repeated Reads of the same file (within one worker)

**Evidence.** `test-http2-padding-aligned` worker ran 168 Read calls; the top
files:
```
84× ext/node/polyfills/http2.ts        (file is 124 KB ≈ 31 K tokens)
34× ext/node/ops/http2/session.rs      (78 KB ≈ 20 K tokens)
17× ext/node/ops/http2/stream.rs       (17 KB ≈ 4 K tokens)
 9× nghttp2/lib/nghttp2_session.c      (vendored C source)
```

All 84 http2.ts reads use `offset/limit` (good — no full-file reloads), but they
re-read overlapping line ranges (e.g. `offset 880 limit 50` followed by `offset
880 limit 15` followed by `offset 915 limit 15`). Each Read result is cached in
the conversation but the model is asking again instead of scrolling its context.

Other workers show similar but weaker patterns:
- `test-http2-https-fallback`: 28× `tls_wrap.rs`
- `test-http2-session-settings`: 34× `session.rs`
- `test-https-agent-session-reuse`: 16× `_tls_wrap.js`

**Recommendation — light prompt nudge.** Add to `prompt.md`:
> Before re-reading a file, scroll your context — you may already have the lines.
> Re-read only when the file has been edited or the prior read was a different range.

Could also add to the worker's CLAUDE.md (`/Users/divy/cc/CLAUDE.md` for
orchestrator's project, but **workers inherit `/Users/divy/src/deno/CLAUDE.md`**
which is 11 KB of mostly-irrelevant Deno onboarding). Trimming that file is
covered separately below.

**Estimated savings:** assuming 30% of redundant slice-Reads can be elided:
~500 K tokens per long-running task; ~10 M tokens/month fleet-wide.

---

## 4. Respawn nudge: boilerplate pasted verbatim every cycle

**Evidence.** `tick.py:1272-1284` builds a ~1500-byte feedback message pasted
into the worker session on every respawn. Sample (one of 285):

```
PR #33563 (https://github.com/denoland/deno/pull/33563) has new activity.
Investigate and address EVERYTHING:
- gh pr view 33563 --repo denoland/deno --comments
- gh pr checks 33563 --repo denoland/deno
- For failing checks: gh run view --log-failed --repo denoland/deno <run-id>
- Inline review threads: gh api repos/denoland/deno/pulls/33563/comments
Counts now: 0 failing checks, 0 issue comments, 0 reviews, 0 inline review comments.
Address every reviewer comment, fix every failing check. Verify locally with
`nix develop -c cargo test --test node_compat -- test-worker-...`. Commit AND push
immediately. Commit MUST include trailer `Co-authored-by: Divy Srivastava
<me@littledivy.com>` (use HEREDOC: `git add -A && git commit -m "$(printf
'%s\n\nCo-authored-by: Divy Srivastava <me@littledivy.com>' '<msg>')" && git
push bot HEAD`). When everything is addressed, print exactly: `<<NODE_BOT_DONE>>
<one-line summary>`.
```

Total nudge bytes across fleet: 430,892 (~108 K tokens). Not huge in absolute
terms but every line of this is in the worker's context for the rest of the
session AND gets replayed on every subsequent resume.

**Recommendation — collapse to a kernel.**
- The `gh` cmd hints, the HEREDOC commit example, and the trailer instruction
  are already in `prompt.md`. The worker has them.
- The nudge can shrink to ~200 bytes:
  > PR #33563 has new activity (counts: 0/0/0/0). Address everything per
  > prompt.md §6, push, then `<<NODE_BOT_DONE>> <summary>`.

**Estimated savings:** ~80 K tokens fleet-wide so far, ~5 K per future respawn.
Nice-to-have but small.

---

## 5. `task_reminder` empty pings inflate every long worker

**Evidence.** Each worker session contains many `attachment` entries of type
`task_reminder`, e.g. 89 in `test-http2-padding-aligned`. Fleet-wide: **1,689
of these.** Body is consistently `{"type":"task_reminder","content":[],"itemCount":0}` —
i.e. the harness is reminding the agent to use TaskCreate even when there's
nothing tracked. The model sees the full `<system-reminder>The task tools
haven't been used recently…</system-reminder>` text (~330 chars) for each.

Workers don't really need TaskCreate — they're single-flow fixers, not
multi-step planners.

**Recommendation.** Disable the task-tool reminder for worker sessions. Two
ways:
- Configure-out via Claude Code settings (operator-level or per-project hook).
- Have the worker call `TaskCreate` once with a single placeholder item; the
  reminder fires only when stale.

**Estimated savings:** ~140 K tokens already; ~1 K per long task going forward.
Nuisance-tier but trivially fixable.

---

## 6. Worker spawn-time fixed overhead

**Evidence.** Every fresh `claude --session-id <new>` worker spawn loads:
- `prompt.md` (4227 bytes ≈ 1.1 K tokens) — pasted as first user message
- `/Users/divy/src/deno-wt/<task>/CLAUDE.md` (11,294 bytes ≈ 2.8 K tokens) —
  this is `denoland/deno`'s repo CLAUDE.md, auto-loaded by Claude Code from cwd
- Anthropic system prompt + tool defs (~10–15 K tokens, model-internal)

Worker-relevant content from the deno CLAUDE.md: maybe the "Building Deno",
"Testing", and "Debugging" sections (~3 KB total). The rest — git workflow
guidance, "spec" tests schema, troubleshooting build failures, performance
tuning — is overhead for a worker whose entire job is `cargo test --test
node_compat`.

**Recommendation — task-scoped CLAUDE.md.**
Inject a slimmed-down `CLAUDE.md` into each worker's worktree (the orchestrator
already owns the worktree creation in `tick.py:spawn_worker`). Keep only the
sections workers actually need. ~3 KB instead of 11 KB saves 2 K tokens per
spawn.

Across 125+ spawns and 285 respawns (resume re-reads CLAUDE.md too): **~800 K
tokens saved**, plus better cache hit on the slimmer prefix.

---

## 7. Lower-impact observations

These showed up but aren't worth fixing on their own.

- **Cargo build/test outputs are well-trimmed.** 98% of cargo invocations
  (2,209 / 2,258) pipe through `tail` / `head` / `grep`. Avg trimmed output is
  1.2 KB. **Workers are doing the right thing here** — don't change anything.

- **Thinking signatures take ~18% of session file size** (e.g. 1.23 MB of 6.45
  MB in `test-http2-padding-aligned`). These are encrypted blobs the API
  requires for tool-use continuity. Not removable. Just noting it for context.

- **Reviewer-feedback `gh` calls** (~5 K each for `gh pr view --comments` /
  `gh pr checks`) are needed when respawning. Not bloat.

- **Subagent overhead is negligible**: 8 subagent jsonls, 430 KB total across
  the fleet. Workers rarely use them.

- **`/remote-control` startup chatter**: 4 system messages per worker spawn
  (~600 bytes). Trivial.

---

## Suggested ordering

1. **This week**: Rotate the orchestrator session (or stop the 3600 s
   ScheduleWakeup). One change, biggest savings, low risk.
2. **Next**: Coalesce worker respawns (e.g. min 30 min between same-PR feedback
   pastes). Tweak in `tick.py` poll loop.
3. **Then**: Slim `prompt.md`'s respawn nudge + drop the redundant HEREDOC line.
4. **Optional**: Slim per-worktree CLAUDE.md, suppress `task_reminder`.
5. **Don't bother**: cargo output trimming (already optimal), thinking
   signatures (immutable).
