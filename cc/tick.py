#!/usr/bin/env python3
"""deno-bot orchestrator. Single-file replacement for tick.sh.

One pass per invocation. Idempotent. Drives node-compat (auto-paced, fork PRs)
and unclaw (manual-spawn, same-repo PRs) workstreams.

Run: python3 tick.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

# ── config ────────────────────────────────────────────────────────────────────

os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

ROOT = Path(os.environ.get("ROOT", Path.home() / ".deno-bot"))
DB_PATH = ROOT / "tasks.db"
INBOX = ROOT / "inbox"
QUEUE = ROOT / "queue.txt"
HALT = ROOT / "halt"
LOGS = ROOT / "logs"

DENO = Path(os.environ.get("DENO", Path.home() / "src/deno"))
WT_BASE = Path(os.environ.get("WT_BASE", Path.home() / "src/deno-wt"))
UNCLAW_REPO = Path(Path.home() / "src/unclaw")
UNCLAW_WT_BASE = Path(Path.home() / "src/unclaw-wt")

UPSTREAM_REPO = "denoland/deno"
BOT_USER = "divybot"
BOT_FORK = f"{BOT_USER}/deno"
UNCLAW_UPSTREAM = "denoland/unclaw"
UNCLAW_AUTH_USER = "littledivy"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".npm-packages/bin/claude"))
TMUX_BIN = "/opt/homebrew/bin/tmux"

SOCKET_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "claude-tmux-sockets"
SOCKET = SOCKET_DIR / "deno-bot.sock"

SCCACHE_BIN = "/opt/homebrew/bin/sccache"
SCCACHE_DIR = str(Path.home() / ".cache/sccache")
SCCACHE_CACHE_SIZE = "60G"

# Tmux socket path on REMOTE hosts. Local mini uses SOCKET above. We use a
# stable /tmp path on remotes (TMPDIR varies on macOS, /tmp is fine on Linux).
REMOTE_SOCKET = "/tmp/claude-tmux-sockets/deno-bot.sock"

CONCURRENT_CAP = 5
OPEN_PR_CAP = 15  # max PRs currently open (review/monitoring); merged/closed free a slot
ATTEMPTS_CAP = 5
IDLE_TICKS_CAP = 4

VIEWER_URL = os.environ.get(
    "VIEWER_URL",
    "https://node-test-viewer.deno.dev/results/latest/darwin.json",
)

# Auto-picker filter is now minimal — workers can land Rust changes too. Only skip tests that
# inherently require Node internals (--expose-internals, internal/...) which Deno doesn't expose.
PICKER_SKIP_RE = re.compile(r"^(?!test-http2-)")  # http2-only focus (operator)
TEST_FILE_FLAG_SKIPS = (
    "--expose-internals",
    "internal/",
)

# Bot accounts to filter out of PR change-detection hash so they don't re-engage workers.
BOT_LOGINS_RE = re.compile(
    r"\[bot\]$|^CLAassistant$|^github-actions$|^codecov$|^vercel$|^renovate$|"
    r"^dependabot$|^divybot$"
)

# Sentinel detection patterns. Detector matches in last 80 lines of pane capture.
# Backtick-exclusion avoids false-positives on the prompt instruction text (which wraps the sentinels in backticks).
RE_DONE = re.compile(r"(?:^|[^`])(?:<<NODE_BOT_DONE>>|<>) [^\s]{3,}", re.M)
RE_DONE_TITLE = re.compile(r"(?:<<NODE_BOT_DONE>>|<>)\s+(.+?)\s*$", re.M)
RE_ESCALATE = re.compile(
    r"(?:^|[^`])(?:<<NODE_BOT_ESCALATE>>|<>).*"
    r"(?:duplicate|requires|needs|cannot|impossible|unsupportable|unsupported|"
    r"depends|blocked|escalate|gives up|stuck)"
)
RE_NO_ACTION = re.compile(
    r"(?:^|[^`])(?:<>|<<NODE_BOT_DONE>>).*"
    r"(?:already|flaky|unrelated|no actionable|no action|nothing to fix|moot|no code change)"
)
RE_CI_PASSED = re.compile(
    r"(?:^|[^`])(?:<<NODE_BOT_DONE>>|<>) (?:ci|all|green|passed|done)", re.M
)
RE_FEEDBACK_DONE = re.compile(
    r"(?:^|[^`])(?:<<NODE_BOT_DONE>>|<>) "
    r"(?:ci|all|green|passed|done|fix|feat|chore|refactor|address)"
)


def now_iso() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}")


# ── hosts (vms.toml) ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Host:
    name: str       # unique identifier — used as DB host column, tmux session
                    # prefix, registry key. Defaults to ssh_host when ssh_host
                    # is unique, but multiple Hosts on the same DNS (different
                    # ssh users) MUST set distinct `name`s.
    ssh_host: str   # DNS / IP for ssh; same as `name` for typical single-VM setup
    user: str       # ssh user (ignored when local)
    port: int       # ssh port (ignored when local)
    capacity: int
    clis: tuple[str, ...]
    wt_base: str    # path on the host (may use ~)
    deno_src: str
    build_prefix: str        # e.g. "nix develop -c"; empty when system cargo works
    sccache: bool            # set RUSTC_WRAPPER on this host's tmux server
    sccache_dir: str         # path on the host
    sccache_cache_size: str  # e.g. "60G"
    unclaw_wrap: bool        # if true, prepend `unclaw run --name divybot --` to the cli launch line

    @property
    def is_local(self) -> bool:
        return self.ssh_host in ("localhost", "127.0.0.1")

    @property
    def ssh_target(self) -> str:
        return f"{self.user}@{self.ssh_host}"

    def expand(self, p: str) -> str:
        """Expand ~ to an absolute path. Local: Path.home(). Remote: cached
        result of `echo $HOME` over the persistent shell. Required because
        tmux's `-c <dir>` does chdir() (no shell expansion), and shlex.quote
        on the way through HostShell prevents bash from expanding `~` either."""
        if self.is_local:
            return os.path.expanduser(p)
        if not (p == "~" or p.startswith("~/")):
            return p
        home = _REMOTE_HOME.get(self.name)
        if home is None:
            from host_shell import get_shell
            sh = get_shell(self.name, self.ssh_target, port=self.port)
            res = sh.run(["sh", "-c", "echo $HOME"], timeout=20.0)
            home = res.stdout.strip() or "/root"
            _REMOTE_HOME[self.name] = home
        return p.replace("~", home, 1)


_REMOTE_HOME: dict[str, str] = {}


_LOCAL_DEFAULTS = dict(
    build_prefix="nix develop -c",
    sccache=True,
    sccache_dir=str(Path.home() / ".cache/sccache"),
    sccache_cache_size="60G",
    unclaw_wrap=False,
)


def load_hosts() -> list[Host]:
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:
        log("tomllib unavailable (need py3.11+); falling back to localhost-only")
        return [Host(
            name="localhost", ssh_host="localhost", user="", port=22, capacity=5,
            clis=("claude",),
            wt_base=str(WT_BASE), deno_src=str(DENO),
            **_LOCAL_DEFAULTS,
        )]
    cfg_path = Path(__file__).parent / "vms.toml"
    if not cfg_path.exists():
        return [Host(
            name="localhost", ssh_host="localhost", user="", port=22, capacity=5,
            clis=("claude",),
            wt_base=str(WT_BASE), deno_src=str(DENO),
            **_LOCAL_DEFAULTS,
        )]
    cfg = tomllib.loads(cfg_path.read_text())
    out: list[Host] = []
    for vm in cfg.get("vm", []):
        # `host` in toml is the DNS/IP. `name` defaults to host but can be
        # set distinctly when multiple Hosts share a DNS (different ssh users).
        ssh_host = vm["host"]
        name = vm.get("name", ssh_host)
        out.append(Host(
            name=name,
            ssh_host=ssh_host,
            user=vm.get("user", os.environ.get("USER", "")),
            port=int(vm.get("port", 22)),
            capacity=int(vm.get("capacity", 1)),
            clis=tuple(vm.get("clis", ["claude"])),
            wt_base=vm.get("wt_base", str(WT_BASE)),
            deno_src=vm.get("deno_src", str(DENO)),
            build_prefix=vm.get("build_prefix", ""),
            sccache=bool(vm.get("sccache", False)),
            sccache_dir=vm.get("sccache_dir", "~/.cache/sccache"),
            sccache_cache_size=vm.get("sccache_cache_size", "60G"),
            unclaw_wrap=bool(vm.get("unclaw_wrap", False)),
        ))
    return out


HOSTS: list[Host] = []  # populated by tick() before any worker call
LOCAL_HOST = Host(
    name="localhost", ssh_host="localhost", user="", port=22, capacity=5,
    clis=("claude",), wt_base=str(WT_BASE), deno_src=str(DENO),
    **_LOCAL_DEFAULTS,
)


def host_by_name(name: str) -> Host:
    for h in HOSTS:
        if h.name == name:
            return h
    return LOCAL_HOST


def host_for_task(row: dict) -> Host:
    return host_by_name(row.get("host") or "localhost")


def pick_host_cli(running_counts: dict[str, int]) -> tuple[Host, str] | None:
    """Choose least-loaded host with free capacity. Picks the first listed cli
    on that host. Returns None when everything is full.

    Tie-break prefers REMOTE hosts: localhost is the orchestrator's host and
    we'd rather have remote VMs running real work when both are equally idle.
    Otherwise rare-but-paid bot VMs sit unused while local handles everything."""
    candidates = [h for h in HOSTS if running_counts.get(h.name, 0) < h.capacity]
    if not candidates:
        return None
    candidates.sort(key=lambda h: (running_counts.get(h.name, 0), h.is_local))
    h = candidates[0]
    if not h.clis:
        return None
    return h, h.clis[0]


# ── shell helpers ─────────────────────────────────────────────────────────────


def run(
    *args: str,
    check: bool = False,
    capture: bool = True,
    timeout: int | None = 60,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess. Defaults: capture stdout/stderr as text, don't raise on non-zero."""
    return subprocess.run(
        list(args),
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(cwd) if cwd else None,
        input=input,
    )


def gh_token(user: str = BOT_USER, host: "Host | None" = None) -> str:
    if host is None or host.is_local:
        out = run("gh", "auth", "token", "--user", user)
    else:
        out = host_run(host, "gh", "auth", "token", "--user", user)
    if out.returncode != 0:
        raise SystemExit(f"no gh auth for user {user} on {host.name if host else 'localhost'}")
    return out.stdout.strip()


def gh_json(*args: str, repo: str = UPSTREAM_REPO) -> dict | list | None:
    out = run("gh", *args, "--repo", repo)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


# ── host execution + tmux ─────────────────────────────────────────────────────


def host_run(host: Host, *cmd: str, **kw) -> subprocess.CompletedProcess[str]:
    """Run a command on `host`. Localhost = direct exec. Remote = goes through
    the persistent HostShell (one long-lived ssh+bash per host) so per-command
    overhead is ~50ms instead of ~4s/channel-open."""
    if host.is_local:
        return run(*cmd, **kw)
    from host_shell import get_shell
    cwd = kw.get("cwd")
    env = kw.get("env")
    timeout = kw.get("timeout") or 300
    cp = f"/tmp/ssh-cm-{host.name}-{host.port}"  # mux reuses TCP across one-offs too
    sh = get_shell(host.name, host.ssh_target, port=host.port, control_path=cp)
    res = sh.run(list(cmd),
                 cwd=str(cwd) if cwd else None,
                 env=env,
                 timeout=float(timeout))
    return subprocess.CompletedProcess(
        args=list(cmd), returncode=res.returncode,
        stdout=res.stdout, stderr=res.stderr,
    )


def t(*args: str, host: Host = LOCAL_HOST, capture: bool = True) -> subprocess.CompletedProcess[str]:
    """tmux on a host. Local uses absolute SOCKET; remote uses REMOTE_SOCKET
    and assumes `tmux` is on PATH there."""
    if host.is_local:
        return run(TMUX_BIN, "-S", str(SOCKET), *args, capture=capture)
    return host_run(host, "tmux", "-S", REMOTE_SOCKET, *args, capture=capture)


def tmux_has_session(name: str, host: Host = LOCAL_HOST) -> bool:
    return t("has-session", "-t", name, host=host).returncode == 0


def tmux_kill(name: str, host: Host = LOCAL_HOST) -> None:
    t("kill-session", "-t", name, host=host)


def tmux_capture(session: str, lines: int = 500, host: Host = LOCAL_HOST) -> str:
    out = t("capture-pane", "-p", "-J", "-t", f"{session}:0.0", "-S", f"-{lines}", host=host)
    return out.stdout if out.returncode == 0 else ""


def tmux_paste(session: str, text: str, then_enter: bool = True, host: Host = LOCAL_HOST) -> None:
    t("set-buffer", "-b", "msg", "--", text, host=host)
    # `-p` emits bracketed-paste sequences so Ink-based TUIs (claude/codex)
    # see this as a paste, render the [Pasted Content N chars] placeholder,
    # and treat the subsequent Enter as a submit instead of folding it into
    # the input buffer.
    t("paste-buffer", "-p", "-b", "msg", "-t", f"{session}:0.0", host=host)
    if then_enter:
        time.sleep(1.5)  # let Ink's paste-debounce settle before commit Enter
        t("send-keys", "-t", f"{session}:0.0", "Enter", host=host)


def tmux_send_line(session: str, line: str, host: Host = LOCAL_HOST) -> None:
    t("send-keys", "-t", f"{session}:0.0", "--", line, "Enter", host=host)


def tmux_clear_history(session: str, host: Host = LOCAL_HOST) -> None:
    t("clear-history", "-t", f"{session}:0.0", host=host)


def ensure_remote_control(session: str, attempts: int = 4, host: Host = LOCAL_HOST) -> bool:
    """Send /remote-control and verify it took (claude only).
    No-op for non-claude CLIs since they have no equivalent."""
    for i in range(attempts):
        if "Remote Control active" in tmux_capture(session, lines=80, host=host):
            return True
        tmux_send_line(session, "/remote-control", host=host)
        time.sleep(3 + i * 2)
    return "Remote Control active" in tmux_capture(session, lines=80, host=host)


def tmux_list_unc_sessions() -> list[str]:
    out = t("ls", "-F", "#{session_name}")
    return [s for s in out.stdout.splitlines() if s.startswith("unc-")]


def session_for_dn(task: str) -> str:
    """Mirror tick.sh's `dn-` session naming (truncated to 32 chars after prefix)."""
    cleaned = re.sub(r"[^A-Za-z0-9-]", "-", task)[:32]
    return f"dn-{cleaned}"


def session_for_unc(slug: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9-]", "-", slug)[:32]
    return f"unc-{cleaned}"


# ── db ────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY,
  status TEXT,
  pr_url TEXT,
  branch TEXT,
  attempts INTEGER DEFAULT 0,
  created_at INTEGER,
  updated_at INTEGER,
  last_error TEXT,
  last_hash TEXT,
  idle_ticks INTEGER DEFAULT 0,
  last_pr_hash TEXT,
  repo TEXT,
  host TEXT DEFAULT 'localhost',
  cli TEXT DEFAULT 'claude',
  session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON tasks(status);
"""

# Idempotent schema migrations for existing DBs.
_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN host TEXT DEFAULT 'localhost'",
    "ALTER TABLE tasks ADD COLUMN cli TEXT DEFAULT 'claude'",
    "ALTER TABLE tasks ADD COLUMN session_id TEXT",
]


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def db_init() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


def task_get(task_id: str) -> dict | None:
    with db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def task_update(task_id: str, **fields: object) -> None:
    fields["updated_at"] = int(time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def task_insert(task_id: str, **fields: object) -> None:
    fields.setdefault("created_at", int(time.time()))
    fields.setdefault("updated_at", int(time.time()))
    fields.setdefault("attempts", 1)
    cols = ", ".join(["id", *fields])
    qs = ", ".join("?" * (1 + len(fields)))
    with db() as c:
        c.execute(
            f"INSERT OR REPLACE INTO tasks({cols}) VALUES({qs})",
            (task_id, *fields.values()),
        )


def tasks_with(status: str, exclude_unclaw: bool = False) -> list[dict]:
    sql = "SELECT * FROM tasks WHERE status=?"
    if exclude_unclaw:
        sql += " AND id NOT LIKE 'unclaw:%'"
    with db() as c:
        return [dict(r) for r in c.execute(sql, (status,)).fetchall()]


# ── sentinel detection ────────────────────────────────────────────────────────


def detect_done(pane: str) -> bool:
    return bool(RE_DONE.search("\n".join(pane.splitlines()[-80:])))


def detect_escalate(pane: str) -> bool:
    return bool(RE_ESCALATE.search("\n".join(pane.splitlines()[-80:])))


def detect_no_action(pane: str) -> bool:
    return bool(RE_NO_ACTION.search("\n".join(pane.splitlines()[-20:])))


def detect_ci_passed(pane: str) -> bool:
    return bool(RE_CI_PASSED.search("\n".join(pane.splitlines()[-80:])))


def detect_feedback_done(pane: str) -> bool:
    return bool(RE_FEEDBACK_DONE.search("\n".join(pane.splitlines()[-80:])))


def extract_title(pane: str, max_len: int = 100) -> str:
    """Return the latest sentinel summary line from pane, sanitized.
    Truncates at word boundary if over max_len."""
    last_match = None
    for line in pane.splitlines()[-80:]:
        m = RE_DONE_TITLE.search(line)
        if m:
            last_match = m.group(1)
    if not last_match:
        return ""
    cleaned = re.sub(r"[^\x09\x0a\x0d\x20-\x7e]", "", last_match).strip().rstrip(".")
    if len(cleaned) <= 8:
        return ""
    if len(cleaned) <= max_len:
        return cleaned
    # Truncate at last word boundary before max_len
    cut = cleaned[:max_len]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > max_len // 2 else cut


def extract_body(pane: str, max_len: int = 4000) -> str:
    """Lines after the sentinel become the PR body. Strips TUI noise."""
    lines = pane.splitlines()[-200:]
    after = []
    found = False
    for line in lines:
        if found:
            after.append(line)
        elif re.search(r"(?:<<NODE_BOT_DONE>>|<>) ", line):
            found = True
    if not after:
        return ""
    noise = re.compile(
        r"^\s*(?:[✻✶✢✳·❯⏵]|Tip:|Cooked|Sauté|Crunched|Churned|Pondering|Mulling|"
        r"Cogitat|Architect|Wrangling|Channelling|Nebulizing|Sublimat|Orchestrat|Cogitating)|"
        r"Remote Control active|bypass permissions|────"
    )
    body = "\n".join(l for l in after if l.strip() and not noise.search(l))
    body = re.sub(r"[^\x09\x0a\x0d\x20-\x7e]", "", body)
    return body[:max_len]


# ── git / pr helpers ──────────────────────────────────────────────────────────


def git(*args: str, cwd: str | Path, env: dict | None = None,
        host: Host = LOCAL_HOST) -> subprocess.CompletedProcess[str]:
    if host.is_local:
        return run("git", *args, cwd=cwd, env=env)
    # Remote: route through the persistent shell. Pass cwd + filtered env via
    # HostShell's structured args (no manual quoting required).
    forwarded: dict[str, str] = {}
    if env:
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                  "GIT_COMMITTER_EMAIL", "GH_TOKEN"):
            if k in env:
                forwarded[k] = env[k]
    return host_run(host, "git", *args, cwd=str(cwd), env=forwarded or None)


def git_env(author: str, email: str | None = None) -> dict[str, str]:
    e = os.environ.copy()
    e["GIT_AUTHOR_NAME"] = author
    e["GIT_AUTHOR_EMAIL"] = email or f"{author}@users.noreply.github.com"
    e["GIT_COMMITTER_NAME"] = author
    e["GIT_COMMITTER_EMAIL"] = email or f"{author}@users.noreply.github.com"
    return e


def trust_worktree(wt: Path) -> None:
    run("bash", "/Users/divy/cc/trust.sh", str(wt))


def install_trailer_hook(wt: Path) -> None:
    """Per-worktree git hooks + exclude rules for the worker."""
    git_dir = run("git", "-C", str(wt), "rev-parse", "--git-dir").stdout.strip()
    gd = Path(git_dir) if Path(git_dir).is_absolute() else (wt / git_dir)
    hooks = gd / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    # Auto-append Co-authored-by trailer to every commit.
    (hooks / "prepare-commit-msg").write_text(
        "#!/bin/sh\n"
        "MSG_FILE=\"$1\"\n"
        "TRAILER='Co-authored-by: Divy Srivastava <me@littledivy.com>'\n"
        "grep -qF \"$TRAILER\" \"$MSG_FILE\" || printf '\\n%s\\n' \"$TRAILER\" >> \"$MSG_FILE\"\n"
    )
    (hooks / "prepare-commit-msg").chmod(0o755)
    # Pre-commit: refuse to commit anything under .claude/
    (hooks / "pre-commit").write_text(
        "#!/bin/sh\n"
        "if git diff --cached --name-only | grep -qE '^\\.claude(/|$)'; then\n"
        "  echo 'refusing to commit .claude/ paths — unstaging' >&2\n"
        "  git reset HEAD -- .claude 2>/dev/null\n"
        "fi\n"
        "exit 0\n"
    )
    (hooks / "pre-commit").chmod(0o755)
    # Local exclude (not committed) so .claude/ doesn't appear in `git status`.
    info = gd / "info"
    info.mkdir(parents=True, exist_ok=True)
    excl = info / "exclude"
    existing = excl.read_text() if excl.exists() else ""
    if ".claude/" not in existing:
        excl.write_text(existing.rstrip() + "\n.claude/\n")


def fetch_pr_signal(pr_num: str, repo: str) -> tuple[str, dict, list]:
    """Return (hash, pr_data, inline_comments) for change detection."""
    pr_data = gh_json(
        "pr", "view", pr_num, "--json",
        "state,statusCheckRollup,reviews,comments,mergeable,mergeStateStatus",
        repo=repo,
    ) or {}
    inline_out = run("gh", "api", f"repos/{repo}/pulls/{pr_num}/comments")
    inline = json.loads(inline_out.stdout) if inline_out.returncode == 0 else []

    sig = {
        "state": pr_data.get("state"),
        "mergeable": pr_data.get("mergeable"),
        "mergeStateStatus": pr_data.get("mergeStateStatus"),
        "ci_fail": [
            c["name"] for c in pr_data.get("statusCheckRollup") or []
            if c.get("conclusion") == "FAILURE"
        ],
        "comments": [
            {"body": c["body"], "a": c["author"]["login"]}
            for c in pr_data.get("comments") or []
            if not BOT_LOGINS_RE.search(c["author"]["login"])
        ],
        "reviews": [
            {"state": r["state"], "body": r.get("body", ""), "a": r["author"]["login"]}
            for r in pr_data.get("reviews") or []
            if not BOT_LOGINS_RE.search(r["author"]["login"])
        ],
    }
    inline_sig = [
        {"body": c["body"], "u": c["user"]["login"], "path": c.get("path"), "line": c.get("line")}
        for c in inline if not BOT_LOGINS_RE.search(c["user"]["login"])
    ]
    import hashlib
    blob = json.dumps(sig, sort_keys=True) + "\n" + json.dumps(inline_sig, sort_keys=True)
    h = hashlib.sha1(blob.encode()).hexdigest()
    return h, pr_data, inline


def pr_counts(pr_data: dict, inline: list) -> dict[str, int]:
    return {
        "fail": sum(1 for c in pr_data.get("statusCheckRollup") or [] if c.get("conclusion") == "FAILURE"),
        "pend": sum(1 for c in pr_data.get("statusCheckRollup") or [] if c.get("status") in ("IN_PROGRESS", "QUEUED") or c.get("state") == "PENDING"),
        "comments": sum(1 for c in pr_data.get("comments") or [] if not BOT_LOGINS_RE.search(c["author"]["login"])),
        "reviews": sum(1 for r in pr_data.get("reviews") or [] if not BOT_LOGINS_RE.search(r["author"]["login"])),
        "inline": sum(1 for c in inline if not BOT_LOGINS_RE.search(c["user"]["login"])),
        "conflict": 1 if pr_data.get("mergeable") == "CONFLICTING" or pr_data.get("mergeStateStatus") == "DIRTY" else 0,
    }


# ── post_worker (node-compat: commit, push, open PR, hand off CI watch) ──────


def post_worker(task: str) -> None:
    row = task_get(task) or {}
    host = host_for_task(row)
    wt = Path(host.expand(host.wt_base)) / task
    branch = row.get("branch") or f"claude/{task}"
    session = session_for_dn(task)
    pane = tmux_capture(session, host=host)
    title = extract_title(pane) or f"fix(ext/node): enable {task}"

    existing_pr = row.get("pr_url") or ""

    # No-diff handling: if PR exists, push any local commits; else abandon.
    has_uncommitted = (
        git("diff", "--quiet", "HEAD", cwd=wt, host=host).returncode != 0
        or bool(git("status", "--porcelain", cwd=wt, host=host).stdout.strip())
    )
    if not has_uncommitted:
        if not existing_pr:
            log(f"no diff: {task}")
            task_update(task, status="abandoned", last_error="no diff")
            tmux_kill(session, host=host)
            if host.is_local:
                shutil.rmtree(wt / "target", ignore_errors=True)
            return
        log(f"no uncommitted diff but PR exists — pushing any local commits: {task}")

    env = git_env(BOT_USER)
    if host.is_local:
        # Local: keep token-in-URL pattern (works whether or not gh auth setup-git ran).
        token = gh_token(BOT_USER)
        env["GH_TOKEN"] = token
        bot_url = f"https://x-access-token:{token}@github.com/{BOT_FORK}.git"
    else:
        # Remote: rely on the VM's `gh auth setup-git` credential helper.
        bot_url = f"https://github.com/{BOT_FORK}.git"

    # Commit if there's anything to commit (no-op otherwise)
    git("add", "-A", cwd=wt, host=host)
    commit_msg = (
        f"{title}\n\nEnables tests/node_compat/runner/suite/test/parallel/{task}.js\n"
        f"\nCo-authored-by: Divy Srivastava <me@littledivy.com>"
    )
    git("commit", "-m", commit_msg, cwd=wt, env=env, host=host)

    # Ensure bot remote exists
    if git("remote", "get-url", "bot", cwd=wt, host=host).returncode != 0:
        git("remote", "add", "bot", bot_url, cwd=wt, host=host)

    git("push", "-u", "bot", branch, cwd=wt, env=env, host=host)

    if existing_pr:
        pr_url = existing_pr
        log(f"PR updated: {pr_url}")
    else:
        body = (
            f"## Summary\n\nEnables `{task}` in node_compat suite.\n\n"
            f"## Test plan\n- [x] `cargo test --test node_compat -- {task}`"
        )
        # gh pr create runs from mini (orchestrator), which always has divybot auth.
        mini_token = gh_token(BOT_USER)
        out = run(
            "gh", "pr", "create", "--repo", UPSTREAM_REPO,
            "--head", f"{BOT_USER}:{branch}", "--title", title, "--body", body,
            env={**env, "GH_TOKEN": mini_token},
        )
        pr_url = out.stdout.strip().splitlines()[-1] if out.returncode == 0 else ""
        log(f"PR: {pr_url}")

    task_update(task, status="review", pr_url=pr_url, last_pr_hash="")
    tmux_kill(session, host=host)
    if host.is_local:
        shutil.rmtree(wt / "target", ignore_errors=True)
    log(f"PR opened, session killed; review-poll handles CI/comments/conflicts: {task}")


# ── poll loops ────────────────────────────────────────────────────────────────


def deliver_inbox() -> None:
    if not INBOX.exists():
        return
    for msg in INBOX.glob("*.txt"):
        task = msg.stem
        row = task_get(task) or {}
        host = host_for_task(row)
        session = session_for_dn(task)
        if not tmux_has_session(session, host=host):
            log(f"inbox msg for {task} but no live session; leaving")
            continue
        tmux_paste(session, msg.read_text(), host=host)
        log(f"delivered inbox msg to {task}")
        task_update(task, idle_ticks=0)
        msg.unlink()


def poll_running() -> None:
    for row in tasks_with("running", exclude_unclaw=True):
        task = row["id"]
        host = host_for_task(row)
        session = session_for_dn(task)
        if not tmux_has_session(session, host=host):
            log(f"session dead: {task} (host={host.name})")
            task_update(task, status="failed", last_error="session died")
            if host.is_local:
                shutil.rmtree(WT_BASE / task / "target", ignore_errors=True)
            continue
        pane = tmux_capture(session, host=host)
        # No-action takes priority over DONE — worker may say "<<NODE_BOT_DONE>> ... flaky/unrelated/no code change",
        # which semantically means "nothing to do" not "ship this".
        if detect_no_action(pane):
            handle_no_action(task)
            continue
        if detect_done(pane):
            log(f"DONE: {task}")
            post_worker(task)
            continue
        if detect_escalate(pane):
            log(f"ESCALATE: {task}")
            task_update(task, status="abandoned", last_error="escalate")
            tmux_kill(session, host=host)
            if host.is_local:
                shutil.rmtree(WT_BASE / task / "target", ignore_errors=True)
            continue
        # Idle detection
        import hashlib
        h = hashlib.sha1("\n".join(pane.splitlines()[-50:]).encode()).hexdigest()
        prev = row.get("last_hash") or ""
        idle = row.get("idle_ticks") or 0
        if h == prev:
            idle += 1
            task_update(task, idle_ticks=idle)
            if idle >= IDLE_TICKS_CAP:
                log(f"idle {IDLE_TICKS_CAP} ticks, killing: {task}")
                task_update(task, status="failed", last_error="idle timeout")
                tmux_kill(session, host=host)
                if host.is_local:
                    shutil.rmtree(WT_BASE / task / "target", ignore_errors=True)
            else:
                log(f"thinking ({idle}/{IDLE_TICKS_CAP}): {task}")
        else:
            task_update(task, last_hash=h, idle_ticks=0)
            log(f"active: {task}")


def handle_no_action(task: str) -> None:
    row = task_get(task) or {}
    pr = row.get("pr_url") or ""
    repo = row.get("repo") or UPSTREAM_REPO
    prev_err = row.get("last_error") or ""
    session = session_for_dn(task)
    host = host_for_task(row)
    if not pr:
        log(f"no-action: {task} → abandoned (worker says already fixed/moot)")
        task_update(task, status="abandoned", last_error="no-action: already fixed/moot")
        tmux_kill(session, host=host)
        if host.is_local:
            shutil.rmtree(WT_BASE / task / "target", ignore_errors=True)
        return

    log(f"no-action: {task} → review (worker says PR is fine; storing hash)")
    pr_num = pr.rsplit("/", 1)[-1]

    # Try to rerun failed checks (will fail without admin — that's OK).
    sc = gh_json("pr", "view", pr_num, "--json", "statusCheckRollup", repo=repo) or {}
    run_ids = set()
    for c in sc.get("statusCheckRollup") or []:
        if c.get("conclusion") == "FAILURE" and (url := c.get("detailsUrl")):
            m = re.search(r"/runs/(\d+)", url)
            if m:
                run_ids.add(m.group(1))
    for rid in run_ids:
        r = run("gh", "run", "rerun", rid, "--failed", "--repo", repo)
        if r.returncode != 0 and r.stderr:
            log(r.stderr.strip().splitlines()[0])

    # First no-action verdict on this PR → ping operator IF there are actual failing checks.
    # If PR is already green, no ping needed — operator can just merge.
    failing_checks = [c for c in (sc.get("statusCheckRollup") or []) if c.get("conclusion") == "FAILURE"]
    if not prev_err.startswith("no-action:") and failing_checks:
        pane = tmux_capture(session, host=host)
        recent = "\n".join(pane.splitlines()[-20:])
        m = re.search(r"<>(.*)", recent)
        reason = (m.group(1).strip()[:500] if m else "")
        body = (
            f"@littledivy heads-up: the bot's analysis of this PR's CI failures says they look "
            f"unrelated/flaky and not addressable from this PR's diff. Verdict:\n\n"
            f"> {reason}\n\n"
            f"Please verify and either rerun the failing checks (admin needed), waive them, or "
            f"merge if the green checks are sufficient. Pinged once; the bot won't re-engage on "
            f"the same signals."
        )
        run("gh", "pr", "comment", pr_num, "--repo", repo, "--body", body)
        log(f"pinged @littledivy on {repo} #{pr_num}")
    elif not failing_checks:
        log(f"no-action: {task} all-green, no ping needed — ready for merge")

    # Compute current hash and store so review-poll stays silent.
    h, _, _ = fetch_pr_signal(pr_num, repo)
    task_update(task, status="review", last_error="no-action: pinged @littledivy", last_pr_hash=h)
    tmux_kill(session, host=host)
    if host.is_local:
        shutil.rmtree(WT_BASE / task / "target", ignore_errors=True)


def poll_monitoring() -> None:
    """Big mode is gone. Transition any leftover 'monitoring' tasks to 'review' + kill session."""
    for row in tasks_with("monitoring"):
        task = row["id"]
        log(f"monitoring → review (Big mode removed): {task}")
        task_update(task, status="review", last_pr_hash="")
        if task.startswith("unclaw:"):
            tmux_kill(session_for_unc(task.removeprefix("unclaw:")))
        else:
            tmux_kill(session_for_dn(task), host=host_for_task(row))


def poll_unclaw() -> None:
    """Stateless poll over live unc-* tmux sessions."""
    for session in tmux_list_unc_sessions():
        slug = session.removeprefix("unc-").rstrip("-")
        wt = UNCLAW_WT_BASE / slug
        branch = f"claude/{slug}"
        task_id = f"unclaw:{slug}"
        if not wt.exists():
            log(f"unclaw worktree gone, killing session: {session}")
            tmux_kill(session)
            continue
        pane = tmux_capture(session)
        row = task_get(task_id) or {}
        cur_status = row.get("status") or ""
        cur_pr = row.get("pr_url") or ""

        # Phase: respawn-for-feedback (PR exists + status='running')
        if cur_status == "running" and cur_pr:
            if detect_feedback_done(pane):
                log(f"unclaw feedback fix done: {slug} (returning to monitoring)")
                task_update(task_id, status="monitoring", last_pr_hash="", last_error="")
                tmux_kill(session)
                continue
            if detect_escalate(pane):
                log(f"unclaw feedback escalated: {slug}")
                task_update(task_id, status="review", last_error="feedback escalated")
                tmux_kill(session)
                continue
            log(f"unclaw addressing feedback: {slug}")
            continue

        # Big mode removed; transition leftover 'monitoring' to 'review'.
        if cur_status == "monitoring":
            log(f"unclaw monitoring → review (Big mode removed): {slug}")
            task_update(task_id, status="review", last_pr_hash="")
            tmux_kill(session)
            continue

        # Phase: initial-fix — DONE → commit + push + open PR + hand off to CI watch
        if detect_done(pane):
            title = extract_title(pane) or f"chore: {slug}"
            body = extract_body(pane) or (
                f"Spawned by deno-bot orchestrator for unclaw workstream. "
                f"Worker did not provide a body.\n\nWorktree branch: {branch}"
            )
            log(f"unclaw DONE: {slug} → {title}")
            try:
                little_token = gh_token(UNCLAW_AUTH_USER)
            except SystemExit:
                log("unclaw: no littledivy auth — leaving session alive for manual handling")
                continue

            has_diff = (
                git("diff", "--quiet", "HEAD", cwd=wt).returncode != 0
                or bool(git("status", "--porcelain", cwd=wt).stdout.strip())
            )
            if not has_diff:
                log(f"unclaw {slug}: no diff — abandoning")
                tmux_kill(session)
                continue

            env = git_env(UNCLAW_AUTH_USER)
            env["GH_TOKEN"] = little_token
            unc_msg = f"{title}\n\nCo-authored-by: Divy Srivastava <me@littledivy.com>"
            git("commit", "-am", unc_msg, cwd=wt, env=env)
            git("push", "origin", branch, cwd=wt, env=env)

            out = run(
                "gh", "pr", "create", "--repo", UNCLAW_UPSTREAM,
                "--head", branch, "--title", title, "--body", body,
                env={**env, "GH_TOKEN": little_token},
            )
            pr_url = out.stdout.strip().splitlines()[-1] if out.returncode == 0 else ""
            pr_num = pr_url.rsplit("/", 1)[-1] if pr_url else ""
            log(f"unclaw PR: {pr_url}")

            task_insert(
                task_id, status="review", branch=branch, pr_url=pr_url,
                repo=UNCLAW_UPSTREAM, last_pr_hash="",
            )
            tmux_kill(session)
            log(f"unclaw PR open, session killed; review-poll handles CI/comments")
            continue

        if detect_escalate(pane):
            log(f"unclaw ESCALATE/no-action: {slug}")
            tmux_kill(session)
            continue
        log(f"unclaw active: {slug}")


def poll_review() -> None:
    """Watch every open-PR task (review/running/monitoring) for new signals.

    - review: respawn worker via claude --continue + checklist.
    - running: paste an UPDATE message into the live session so worker addresses new feedback alongside what they're already doing.
    - monitoring: same — UPDATE message; CI watch keeps running.
    """
    with db() as c:
        rows = c.execute(
            "SELECT id, status, pr_url, COALESCE(repo,?) AS repo FROM tasks "
            "WHERE status IN ('review','running','monitoring') AND pr_url IS NOT NULL AND pr_url != ''",
            (UPSTREAM_REPO,),
        ).fetchall()

    for row in rows:
        task = row["id"]
        cur_status = row["status"]
        pr_url = row["pr_url"]
        repo = row["repo"]
        pr_num = pr_url.rsplit("/", 1)[-1]
        pr_data = gh_json("pr", "view", pr_num, "--json", "state,statusCheckRollup,reviews,comments,mergeable,mergeStateStatus", repo=repo) or {}
        state = pr_data.get("state")
        if state == "MERGED":
            task_update(task, status="merged")
            if repo == UPSTREAM_REPO:
                run("git", "-C", str(DENO), "worktree", "remove", "--force", str(WT_BASE / task))
            log(f"merged ({repo}): {task}")
            continue
        if state == "CLOSED":
            task_update(task, status="abandoned", last_error="closed unmerged")
            if repo == UPSTREAM_REPO:
                shutil.rmtree(WT_BASE / task / "target", ignore_errors=True)
            log(f"closed ({repo}): {task}")
            continue

        h, _, inline = fetch_pr_signal(pr_num, repo)
        counts = pr_counts(pr_data, inline)
        prev_hash = task_get(task).get("last_pr_hash") or ""
        if h == prev_hash:
            if cur_status == "review" and counts["pend"]:
                log(f"waiting CI: {task} ({counts['pend']} pending)")
            continue

        task_update(task, last_pr_hash=h)

        # Baseline: silent only if nothing actionable.
        if not prev_hash and counts["fail"] == 0 and counts["comments"] == 0 and counts["reviews"] == 0 and counts["inline"] == 0 and not counts.get("conflict"):
            log(f"baseline: {task} (clean)")
            continue

        # Skip re-feedback if worker already verdict'd no-action AND no NEW human/conflict signals.
        last_err = task_get(task).get("last_error") or ""
        if last_err.startswith("no-action:") and counts["comments"] == 0 and counts["reviews"] == 0 and counts["inline"] == 0 and not counts.get("conflict"):
            log(f"no-action acknowledged, skipping feedback: {task}")
            continue

        # If task is currently 'running' or 'monitoring', the worker session is alive — paste an UPDATE
        # message instead of respawning. Don't kill the session.
        if cur_status in ("running", "monitoring"):
            paste_update_to_live_worker(task, repo, pr_num, counts)
            continue

        # status == 'review' — respawn worker. PR is still OPEN here (review-poll only
        # iterates open PRs); operator policy: never abandon while PR is open.
        respawn_worker_for_feedback(task, repo, pr_num, counts)


def paste_update_to_live_worker(task: str, repo: str, pr_num: str, counts: dict[str, int]) -> None:
    """Paste a 'new feedback arrived' message to a worker session that's already alive."""
    row = task_get(task) or {}
    if task.startswith("unclaw:"):
        slug = task.removeprefix("unclaw:")
        session = session_for_unc(slug)
        host = LOCAL_HOST
    else:
        session = session_for_dn(task)
        host = host_for_task(row)
    if not tmux_has_session(session, host=host):
        log(f"live worker session gone for {task}; bouncing to review for next-tick respawn")
        task_update(task, status="review", last_pr_hash="")
        return
    push_remote = "origin" if task.startswith("unclaw:") else "bot"
    conflict_note = ""
    if counts.get("conflict"):
        conflict_note = (
            f"\n⚠️ MERGE CONFLICT — rebase: `git fetch origin && git rebase origin/main` "
            f"(resolve in editor, `git rebase --continue`), then `git push {push_remote} HEAD --force-with-lease`."
        )
    msg = (
        f"NEW activity on PR #{pr_num} (https://github.com/{repo}/pull/{pr_num}) while you were working. "
        f"Counts now: {counts['fail']} failing checks, {counts['comments']} issue comments, "
        f"{counts['reviews']} reviews, {counts['inline']} inline review comments"
        f"{', MERGE CONFLICT' if counts.get('conflict') else ''}. "
        f"Investigate alongside what you're already doing:\n"
        f"- gh pr view {pr_num} --repo {repo} --comments\n"
        f"- gh pr checks {pr_num} --repo {repo}\n"
        f"- gh api repos/{repo}/pulls/{pr_num}/comments\n"
        f"Address the new feedback, commit, and push. If a comment requests a fundamental scope change "
        f"or asks whether the PR is worth landing, print `<<NODE_BOT_ESCALATE>> reviewer questioning PR — operator decides`."
        f"{conflict_note}"
    )
    tmux_paste(session, msg, host=host)
    log(f"pasted update to live worker: {task} (#{pr_num}) — fail={counts['fail']} cmt={counts['comments']} rev={counts['reviews']} inline={counts['inline']}")


def respawn_worker_for_feedback(task: str, repo: str, pr_num: str, counts: dict[str, int]) -> None:
    """Resume the worker session and paste a feedback checklist."""
    from cli_adapters import adapter_for
    row = task_get(task) or {}
    host = host_for_task(row)
    cli_name = row.get("cli") or "claude"
    sid = row.get("session_id") or ""

    if task.startswith("unclaw:"):
        slug = task.removeprefix("unclaw:")
        wt = UNCLAW_WT_BASE / slug
        session = session_for_unc(slug)
        worker_name = f"unclaw:{slug}"
        env_prefix = f"GH_TOKEN=$(gh auth token --user {UNCLAW_AUTH_USER}) "
        verify = (
            "Verify the fix locally (run the repo's tests/lints). "
            "Commit AND push immediately. Commit MUST include trailer "
            "`Co-authored-by: Divy Srivastava <me@littledivy.com>` (use HEREDOC: "
            "`git add -A && git commit -m \"$(printf '%s\\n\\nCo-authored-by: Divy Srivastava <me@littledivy.com>' '<msg>')\" && git push origin HEAD`)."
        )
    else:
        wt = (WT_BASE / task) if host.is_local else Path(host.wt_base) / task
        session = session_for_dn(task)
        worker_name = f"deno-bot:{task}"
        env_prefix = ""
        verify = (
            f"Verify locally with `nix develop -c cargo test --test node_compat -- {task}`. "
            "Commit AND push immediately. Commit MUST include trailer "
            "`Co-authored-by: Divy Srivastava <me@littledivy.com>` (use HEREDOC: "
            "`git add -A && git commit -m \"$(printf '%s\\n\\nCo-authored-by: Divy Srivastava <me@littledivy.com>' '<msg>')\" && git push bot HEAD`)."
        )

    cli = adapter_for(cli_name)

    if not tmux_has_session(session, host=host):
        # Worktree existence check: local can stat directly; remote uses ssh test.
        if host.is_local:
            if not wt.exists():
                log(f"worktree gone, can't resume {task}")
                return
            trust_worktree(wt)
            install_trailer_hook(wt)
        else:
            chk = host_run(host, "test", "-d", str(wt))
            if chk.returncode != 0:
                log(f"worktree gone on {host.name}, can't resume {task}")
                return
            if cli_name == "claude":
                trust_worktree_remote(host, str(wt))
        t("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", str(wt), host=host)
        # Prefer cli-specific resume; fall back to cli.launch for clis that don't resume.
        resume_cmd = cli.resume(sid, task) if sid else None
        if resume_cmd is None and cli_name == "claude":
            # legacy claude with no session_id stored: --continue picks last in cwd
            resume_cmd = f"{cli.bin} --continue --permission-mode bypassPermissions -n '{worker_name}'"
        inner = resume_cmd or cli.launch(sid, task)
        if host.unclaw_wrap:
            inner = f"unclaw run --name {BOT_USER} -- {inner}"
        cmd = f"{env_prefix}{inner}"
        tmux_send_line(session, cmd, host=host)
        time.sleep(6)
        if cli.supports_remote_control():
            ensure_remote_control(session, host=host)
        tmux_clear_history(session, host=host)

    push_remote = "origin" if task.startswith("unclaw:") else "bot"
    conflict_block = ""
    if counts.get("conflict"):
        conflict_block = (
            f"\n\n⚠️ MERGE CONFLICT against base branch. Resolve first:\n"
            f"  git fetch origin\n"
            f"  git rebase origin/main      # or `git merge origin/main`\n"
            f"  # resolve conflicts in editor\n"
            f"  git add -A && git rebase --continue\n"
            f"  git push {push_remote} HEAD --force-with-lease\n"
            f"After conflicts are resolved AND any failing checks are fixed, signal done."
        )
    fb = (
        f"PR #{pr_num} (https://github.com/{repo}/pull/{pr_num}) has new activity. Investigate and address EVERYTHING:\n"
        f"- gh pr view {pr_num} --repo {repo} --comments\n"
        f"- gh pr checks {pr_num} --repo {repo}\n"
        f"- For failing checks: gh run view --log-failed --repo {repo} <run-id>\n"
        f"- Inline review threads: gh api repos/{repo}/pulls/{pr_num}/comments\n"
        f"Counts now: {counts['fail']} failing checks, {counts['comments']} issue comments, "
        f"{counts['reviews']} reviews, {counts['inline']} inline review comments"
        f"{', MERGE CONFLICT' if counts.get('conflict') else ''}.\n"
        f"Address every reviewer comment, fix every failing check. {verify} "
        f"When everything is addressed, print exactly: `<<NODE_BOT_DONE>> <one-line summary>`."
        f"{conflict_block}"
    )
    tmux_paste(session, fb, host=host)
    with db() as c:
        c.execute(
            "UPDATE tasks SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
            (int(time.time()), task),
        )
    log(f"fed back: {task} ({pr_num}) — fail={counts['fail']} cmt={counts['comments']} rev={counts['reviews']} inline={counts['inline']}")


# ── picker + spawn ────────────────────────────────────────────────────────────


def fetch_failing_tests() -> list[str]:
    try:
        with urllib.request.urlopen(VIEWER_URL, timeout=20) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    return [
        re.sub(r"\.js$", "", k.removeprefix("parallel/"))
        for k, v in data.get("results", {}).items()
        if k.startswith("parallel/") and v[0] is False
    ]


def pick_task() -> str | None:
    config = DENO / "tests/node_compat/config.jsonc"
    tests_dir = DENO / "tests/node_compat/runner/suite/test/parallel"
    if not tests_dir.exists():
        log("tests dir missing")
        sys.exit(1)
    config_text = config.read_text() if config.exists() else ""

    def candidate_ok(name: str) -> bool:
        f = tests_dir / f"{name}.js"
        if not f.exists():
            return False
        if f'"parallel/{name}.js"' in config_text:
            return False
        if task_get(name):
            return False
        head = "\n".join(f.read_text(errors="ignore").splitlines()[:3])
        if any(flag in head for flag in TEST_FILE_FLAG_SKIPS):
            return False
        return True

    # Forced queue first
    if QUEUE.exists():
        forced = QUEUE.read_text().splitlines()
        for f in forced:
            f = f.strip()
            if not f:
                continue
            row = task_get(f)
            if row and row.get("status") not in (None, "failed", "abandoned"):
                continue
            QUEUE.write_text("\n".join(x for x in forced if x.strip() != f) + "\n")
            log(f"forced from queue: {f}")
            return f

    # Viewer-based picker with safety filter
    candidates = fetch_failing_tests()
    if candidates:
        log(f"viewer: {len(candidates)} failing parallel tests")
        candidates = [c for c in candidates if not PICKER_SKIP_RE.search(c)]
    else:
        log("viewer unreachable — falling back to alphabetical scan")
        candidates = sorted(p.stem for p in tests_dir.glob("*.js"))

    for name in candidates:
        if not candidate_ok(name):
            continue
        # Dup-check: skip if upstream has open PR for this exact test
        out = gh_json(
            "pr", "list", "--state", "open", "--search",
            f'"parallel/{name}.js"', "--json", "number,author",
            repo=UPSTREAM_REPO,
        )
        if out and any(pr["author"]["login"] != BOT_USER for pr in out):
            log(f"skip {name} — upstream open PR exists")
            task_insert(name, status="abandoned", last_error="duplicate of upstream PR")
            continue
        return name
    return None


def _running_counts_per_host() -> dict[str, int]:
    counts: dict[str, int] = {}
    with db() as c:
        for r in c.execute("SELECT host, COUNT(*) c FROM tasks WHERE status='running' GROUP BY host"):
            counts[r["host"] or "localhost"] = r["c"]
    return counts


def trust_worktree_remote(host: Host, wt_path: str) -> None:
    """Add wt_path to the remote ~/.claude.json projects map."""
    py = (
        "import json,os,sys;p=os.path.expanduser('~/.claude.json');"
        "open(p,'a').close() if not os.path.exists(p) else None;"
        "d=json.load(open(p)) if os.path.getsize(p) else {};"
        "d.setdefault('projects',{}).setdefault(sys.argv[1],{})['hasTrustDialogAccepted']=True;"
        "d['remoteDialogSeen']=True;"
        "json.dump(d,open(p,'w'),indent=2)"
    )
    host_run(host, "python3", "-c", py, wt_path)


def spawn_worker(task: str) -> None:
    from cli_adapters import adapter_for
    import uuid as _uuid

    counts = _running_counts_per_host()
    pick = pick_host_cli(counts)
    if not pick:
        log("no host with capacity; skipping spawn")
        return
    host, cli_name = pick
    cli = adapter_for(cli_name)

    # Worker path: ~ stays raw on remote (ssh expands it); expand here for local.
    base_str = host.expand(host.wt_base)
    deno_str = host.expand(host.deno_src)
    wt_str = f"{base_str}/{task}"
    wt_local = Path(wt_str) if host.is_local else None
    branch = f"claude/{task}"
    session = session_for_dn(task)
    sid = str(_uuid.uuid4())

    # Worktree creation on the worker's host.
    host_run(host, "git", "-C", deno_str, "fetch", "origin", "main", "--quiet")
    add = host_run(host, "git", "-C", deno_str, "worktree", "add", "-B", branch, wt_str, "origin/main")
    if add.returncode != 0:
        host_run(host, "git", "-C", deno_str, "worktree", "add", wt_str, branch)
    git("config", "user.name", BOT_USER, cwd=wt_str, host=host)
    git("config", "user.email", f"{BOT_USER}@users.noreply.github.com", cwd=wt_str, host=host)

    if host.is_local and wt_local:
        install_trailer_hook(wt_local)
        trust_worktree(wt_local)
    elif cli_name == "claude":
        # Claude on a remote VM still needs the workspace pre-trusted there.
        trust_worktree_remote(host, wt_str)

    tmux_kill(session, host=host)
    t("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", wt_str, host=host)
    launch = cli.launch(sid, task)
    if host.unclaw_wrap:
        launch = f"unclaw run --name {BOT_USER} -- {launch}"
    tmux_send_line(session, launch, host=host)
    time.sleep(8)
    if cli.supports_remote_control():
        ensure_remote_control(session, host=host)

    prompt_template = (Path(__file__).parent / "prompt.md").read_text()
    file_hint = re.sub(r"^test-([a-z0-9]*).*", r"\1", task)
    prompt = (
        prompt_template
        .replace("<NAME>", task)
        .replace("<file>", file_hint)
        .replace("{{BUILD_PREFIX}}", host.build_prefix)
    )
    tmux_paste(session, prompt, host=host)

    task_insert(task, status="running", branch=branch, host=host.name, cli=cli_name, session_id=sid)
    log(f"spawned: {task} on {host.name} via {cli_name} (sid={sid[:8]})")


# ── main ──────────────────────────────────────────────────────────────────────


def tick() -> None:
    global HOSTS
    HOSTS = load_hosts()

    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    WT_BASE.mkdir(parents=True, exist_ok=True)

    # Per-host sccache: every host shares one S3-style cache (MinIO at
    # sccache.littledivy.com). Creds live in ~/.deno-bot/sccache.env (not
    # committed) and are pushed to each host's tmux server. If the file is
    # missing we fall back to the old local-disk cache.
    sccache_env_path = ROOT / "sccache.env"
    shared_env: dict[str, str] = {}
    if sccache_env_path.exists():
        for line in sccache_env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" in line:
                k, v = line.split("=", 1)
                shared_env[k.strip()] = v.strip().strip('"').strip("'")

    for h in HOSTS:
        if not h.sccache:
            continue
        # Skip cap=0 hosts (disabled) — no need to reach unreachable VMs to
        # push env they won't use; one bad ssh would crash the whole tick.
        if h.capacity == 0:
            continue
        sccache_bin = SCCACHE_BIN if h.is_local else "sccache"
        if h.is_local and not Path(SCCACHE_BIN).exists():
            continue
        try:
            # /tmp/claude-tmux-sockets/ is wiped on every VM reboot (tmpfs).
            # tmux refuses to create the socket if the parent dir is gone.
            if not h.is_local:
                host_run(h, "mkdir", "-p", "/tmp/claude-tmux-sockets")
            t("setenv", "-g", "RUSTC_WRAPPER", sccache_bin, host=h)
            if shared_env:
                for k, v in shared_env.items():
                    t("setenv", "-g", k, v, host=h)
            else:
                # legacy local-disk fallback
                if h.is_local:
                    Path(h.sccache_dir.replace("~", str(Path.home()))).mkdir(parents=True, exist_ok=True)
                else:
                    host_run(h, "mkdir", "-p", h.sccache_dir)
                t("setenv", "-g", "SCCACHE_DIR", h.sccache_dir, host=h)
                t("setenv", "-g", "SCCACHE_CACHE_SIZE", h.sccache_cache_size, host=h)
        except Exception as e:
            log(f"sccache env push failed for {h.name}: {e}; skipping host this tick")

    # Remote VMs are unclaw-wrapped (TLS-MITM). cargo's TLS to crates.io
    # gets corrupted (TLS EOF mid-download → rustc ICE on truncated dep).
    # Force CARGO_NET_OFFLINE=true so builds hit the local registry cache
    # populated by `cargo fetch` at bootstrap. Localhost stays online.
    for h in HOSTS:
        if h.is_local or h.capacity == 0:
            continue
        try:
            t("setenv", "-g", "CARGO_NET_OFFLINE", "true", host=h)
            # Bot VMs are 8GB; default cargo parallelism × rustc memory
            # blows past on the link step. Cap to 2 parallel jobs.
            t("setenv", "-g", "CARGO_BUILD_JOBS", "2", host=h)
        except Exception as e:
            log(f"remote env push failed for {h.name}: {e}")

    if HALT.exists():
        log("halted")
        return

    if not (DENO / ".git").exists():
        log("cloning deno...")
        run("git", "clone", "--depth", "200", "https://github.com/denoland/deno", str(DENO))

    db_init()

    deliver_inbox()
    poll_running()
    poll_monitoring()
    poll_unclaw()
    poll_review()
    deliver_inbox()  # second pass: catches inbox msgs whose worker just got respawned

    # Spawn until every host is full, the open-PR cap is hit, or queue dries.
    # Operator preference: keep capacity always saturated when there's work.
    total_capacity = sum(h.capacity for h in HOSTS)
    while True:
        with db() as c:
            open_prs = c.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('review','monitoring')"
            ).fetchone()[0]
        if open_prs >= OPEN_PR_CAP:
            log(f"open PR cap ({open_prs})")
            break
        counts = _running_counts_per_host()
        total_running = sum(counts.values())
        if total_running >= total_capacity:
            log(f"all hosts full ({total_running}/{total_capacity})")
            break
        task = pick_task()
        if not task:
            log("no fresh tasks")
            break
        try:
            spawn_worker(task)
        except Exception as e:
            # A host's ssh died mid-spawn or some other transient error. Don't
            # crash the whole tick — log + continue. The task was already
            # popped from the queue; re-queue it so we don't lose it.
            log(f"spawn failed for {task}: {e}; re-queueing")
            with QUEUE.open("a") as f:
                f.write(task + "\n")
            break  # bail this tick to avoid hammering a broken host


if __name__ == "__main__":
    tick()
