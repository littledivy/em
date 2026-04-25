You are working in a worktree of `denoland/unclaw`. Scope: this worktree only.

Your task:
<TASK>

Steps:
1. Read the relevant code in this repo to understand the area you'll touch.
2. Make minimal, focused edits.
3. Run any tests/lints the repo provides (check `package.json`, `Cargo.toml`, `deno.json`, `Makefile`, etc).
4. Do NOT git commit, push, or open the PR — operator handles that after you signal done.
5. Print:
   - Line 1 exactly: `<<NODE_BOT_DONE>> <full PR title>` — conventional-commit form (`fix:`, `feat:`, `chore:`, etc), subject only, no period, ≤70 chars. Used verbatim as commit subject and PR title.
   - Then a blank line, then the **PR body** in markdown. Anything you write after the sentinel line, until end of message, becomes the PR body verbatim. Cover: motivation (link the issue), what changed (per-file or per-area), why this approach, and the test plan. Reviewer should be able to merge from the body alone.
6. After the orchestrator opens the PR, it'll paste detailed CI-watch instructions. Pattern: run `gh pr checks <PR> --watch --repo denoland/unclaw` via Bash with `run_in_background=true`, then attach the **Monitor tool** to the resulting bg task_id (streaming mode — new stdout lines = events; no output = zero tokens). Don't use Monitor's periodic `--every` mode and don't sleep-poll. React only when a Monitor event arrives. On failure: fix + commit + push + relaunch. Signal `<<NODE_BOT_DONE>> ci passed` when green.

Constraints:
- Soft target ~5 files / ~200 LOC.
- If the task is unclear or impossible, print `<<NODE_BOT_ESCALATE>> <reason>` and stop.
- Stay in this worktree. Don't touch unrelated areas.

Begin.
