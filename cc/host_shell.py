"""Persistent ssh shell per remote host, ridden through a local tmux pane.

Why this shape:
  russh (the bot VMs' sshd) does not forward stdin to `ssh host '<cmd>'` exec
  channels nor to `ssh host bash -l` with stdin=PIPE. It DOES forward stdin to
  pty channels (interactive shell). So we put the ssh client inside a local
  tmux session (which gives it a real pty), drive it via `tmux send-keys`,
  and read its output via `tmux capture-pane`. Both ops talk to the LOCAL
  tmux socket — sub-100ms per call.

Per-command framing:
  Each call wraps the user command:
    ( <cmd> ) > /tmp/.tsh_out_$$ 2> /tmp/.tsh_err_$$
    printf '\\nBEGIN_<uid>\\n'; cat out
    printf '\\nSEP_<uid>\\n';   cat err
    printf '\\nEND_<uid>_<rc>\\n'

  The reader polls capture-pane until END_<uid>_ appears, then parses
  stdout / stderr / rc by locating BEGIN/SEP/END on their own lines (the
  echoed command line — which contains the same tokens mid-line — is ignored
  because we anchor on `\\n<token>\\n`). uuid suffix makes collisions
  effectively impossible.

Robustness:
  - ssh dies (server kicked us, network blip): _ensure_alive() detects via
    `tmux has-session` + sentinel pings, respawns the pane and reissues the
    ssh client.
  - Per-command timeout is honored by giving up the wait loop; the next call
    will spot stale state and rebuild.
"""
from __future__ import annotations

import atexit
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# Local tmux socket — same one tick.py uses for orch + worker sessions.
_TMUX_BIN = "/opt/homebrew/bin/tmux"
_LOCAL_SOCK = str(Path(os.environ.get("TMPDIR", "/tmp")) / "claude-tmux-sockets" / "deno-bot.sock")
# Wide pane so long wrapped commands don't fragment sentinels across rows.
_PANE_W, _PANE_H = "1000", "50"


@dataclass
class ShellResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class ShellError(RuntimeError):
    pass


def _tmux(*args: str, capture: bool = True, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_TMUX_BIN, "-S", _LOCAL_SOCK, *args],
        capture_output=capture, text=True, timeout=timeout,
    )


class HostShell:
    """One per host. Holds the tmux session name where the ssh client lives."""

    def __init__(self, host_name: str, ssh_target: str, port: int = 22,
                 control_path: str | None = None) -> None:
        self.host_name = host_name
        self.ssh_target = ssh_target
        self.port = port
        self.control_path = control_path  # accepted for API-compat; unused
        # tmux session names can't contain '.' or ':', and tmux silently
        # rewrites '.' to '_' which then breaks our `kill-session` lookup
        # (we'd send the un-rewritten name and miss the session).
        self.session = f"tsh-{re.sub(r'[^A-Za-z0-9_-]', '_', host_name)}"
        self._lock = threading.Lock()
        self._alive = False

    # ---- lifecycle ----

    def _spawn_session(self) -> None:
        # Kill any stale session of this name from a prior run.
        _tmux("kill-session", "-t", self.session, capture=True)
        r = _tmux("new-session", "-d", "-s", self.session, "-x", _PANE_W, "-y", _PANE_H)
        if r.returncode != 0:
            raise ShellError(f"tmux new-session failed: {r.stderr}")
        # Disable LOCAL pty echo so input lines don't appear in the pane and
        # confuse the sentinel parser.
        ssh_cmd = (
            f"ssh -o BatchMode=yes -o ConnectTimeout=10 "
            f"-o ServerAliveInterval=30 -o ServerAliveCountMax=3 "
            f"-p {self.port} {shlex.quote(self.ssh_target)}"
        )
        # `-l` (literal) is mandatory: without it, tmux parses each arg as a
        # KEY NAME, so anything that isn't `Enter`/`Up`/etc. is silently
        # dropped — your `echo hi` never reaches the pane.
        _tmux("send-keys", "-t", self.session, "-l", ssh_cmd)
        _tmux("send-keys", "-t", self.session, "Enter")
        # Wait for the remote shell prompt before sending anything else,
        # otherwise our follow-up keystrokes get queued into LOCAL bash's
        # input buffer and discarded once ssh takes over the pty.
        self._wait_for_remote_prompt(timeout=20.0)
        # Flatten PS1 so prompts don't interleave with sentinels in captures.
        _tmux("send-keys", "-t", self.session, "-l", "export PS1=''")
        _tmux("send-keys", "-t", self.session, "Enter")
        time.sleep(0.3)
        time.sleep(0.4)
        _tmux("clear-history", "-t", self.session)
        # Bootstrap a sentinel ping to confirm we're talking to remote bash.
        out, err, rc = self._send_raw("true", timeout=20.0)
        if rc != 0:
            raise ShellError(f"warmup failed (rc={rc}, err={err!r})")
        self._alive = True

    def _wait_for_remote_prompt(self, timeout: float = 20.0) -> None:
        """Poll capture-pane until a remote shell prompt appears (e.g.
        `root@host:~#` or `user@host:...$`). Distinguishes from local prompt
        by requiring `@` followed by a non-mac-mini hostname pattern."""
        deadline = time.time() + timeout
        # Match any standard linux PS1 ending in `$ ` or `# ` AFTER the line
        # containing the remote hostname. We just look for `@<host>:` since
        # the remote bot host is in self.ssh_target.
        host_part = self.ssh_target.split("@", 1)[-1]
        # russh banner contains "Last login: ..." right before the prompt;
        # using both gives us a robust signal.
        while time.time() < deadline:
            cap = _tmux("capture-pane", "-t", self.session, "-p", "-S", "-200")
            if cap.returncode != 0:
                raise ShellError("capture-pane failed during prompt wait")
            pane = self._strip_ansi(cap.stdout)
            # Most reliable: presence of "Last login:" tells us we got past auth.
            if "Last login:" in pane:
                return
            # Fallback: a `:~#` or `:~$` suffix on a line containing host_part.
            if re.search(rf"@{re.escape(host_part)}.*[#$]\s*$", pane, re.M):
                return
            time.sleep(0.3)
        raise ShellError(
            f"timed out waiting for remote prompt; last pane: "
            f"{self._strip_ansi(_tmux('capture-pane', '-t', self.session, '-p', '-S', '-50').stdout)[-300:]!r}"
        )

    def _ensure_alive(self) -> None:
        if self._alive and _tmux("has-session", "-t", self.session).returncode == 0:
            return
        self._alive = False
        self._spawn_session()

    def close(self) -> None:
        with self._lock:
            _tmux("kill-session", "-t", self.session, capture=True)
            self._alive = False

    # ---- send/recv ----

    _CTRL_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")  # strip ANSI CSI

    def _strip_ansi(self, s: str) -> str:
        return self._CTRL_RE.sub("", s)

    def _send_raw(self, raw_bash: str, timeout: float = 300.0) -> tuple[str, str, int]:
        """Submit one wrapped command, wait for sentinels, return (out, err, rc)."""
        uid = uuid.uuid4().hex[:12]
        BEGIN = f"__BEGIN_{uid}__"
        SEP = f"__SEP_{uid}__"
        END = f"__END_{uid}__"
        wrapped = (
            f"( {raw_bash} ) > /tmp/.tsh_out_$$ 2> /tmp/.tsh_err_$$; _RC=$?; "
            f"printf '\\n{BEGIN}\\n'; cat /tmp/.tsh_out_$$ 2>/dev/null; "
            f"printf '\\n{SEP}\\n'; cat /tmp/.tsh_err_$$ 2>/dev/null; "
            f"printf '\\n{END}_%s\\n' \"$_RC\"; "
            f"rm -f /tmp/.tsh_out_$$ /tmp/.tsh_err_$$"
        )
        # Long bodies use set-buffer/paste-buffer for atomic delivery; short
        # ones go via direct send-keys -l (literal — non-literal mode parses
        # args as key names and silently drops command text).
        if len(wrapped) > 1500:
            _tmux("set-buffer", "-b", f"hs_{uid}", "--", wrapped)
            _tmux("paste-buffer", "-b", f"hs_{uid}", "-t", self.session)
            _tmux("send-keys", "-t", self.session, "Enter")
        else:
            _tmux("send-keys", "-t", self.session, "-l", wrapped)
            _tmux("send-keys", "-t", self.session, "Enter")

        end_anchor = f"\n{END}_"
        deadline = time.time() + timeout
        last_pane = ""
        while time.time() < deadline:
            cap = _tmux("capture-pane", "-t", self.session, "-p", "-J", "-S", "-2000")
            if cap.returncode != 0:
                raise ShellError("capture-pane failed (session gone)")
            pane = self._strip_ansi(cap.stdout)
            last_pane = pane
            end_idx = pane.rfind(end_anchor)
            if end_idx >= 0:
                rest = pane[end_idx + len(end_anchor):]
                rc_line = rest.split("\n", 1)[0]
                m = re.match(r"(\d+)\s*$", rc_line.strip())
                if not m:
                    time.sleep(0.1)
                    continue
                rc = int(m.group(1))
                begin_anchor = f"\n{BEGIN}\n"
                sep_anchor = f"\n{SEP}\n"
                begin_idx = pane.rfind(begin_anchor, 0, end_idx)
                if begin_idx < 0:
                    return "", "", rc
                sep_idx = pane.find(sep_anchor, begin_idx, end_idx)
                if sep_idx >= 0:
                    out = pane[begin_idx + len(begin_anchor):sep_idx]
                    err = pane[sep_idx + len(sep_anchor):end_idx]
                else:
                    out = pane[begin_idx + len(begin_anchor):end_idx]
                    err = ""
                if out.endswith("\n"):
                    out = out[:-1]
                if err.endswith("\n"):
                    err = err[:-1]
                return out, err, rc
            time.sleep(0.1)
        raise ShellError(
            f"command timed out after {timeout}s; last 400 chars of pane: {last_pane[-400:]!r}"
        )

    # ---- public API ----

    def run(self, argv: list[str], cwd: str | None = None,
            env: dict[str, str] | None = None,
            timeout: float = 300.0) -> ShellResult:
        with self._lock:
            attempts = 0
            last_err: Exception | None = None
            while attempts < 2:
                attempts += 1
                try:
                    self._ensure_alive()
                    quoted = " ".join(shlex.quote(a) for a in argv)
                    parts: list[str] = []
                    if cwd:
                        parts.append(f"cd {shlex.quote(cwd)}")
                    if env:
                        env_prefix = " ".join(
                            f"{k}={shlex.quote(v)}" for k, v in env.items()
                        )
                        parts.append(f"{env_prefix} {quoted}")
                    else:
                        parts.append(quoted)
                    bash = " && ".join(parts) if cwd else parts[-1]
                    out, err, rc = self._send_raw(bash, timeout=timeout)
                    return ShellResult(args=argv, returncode=rc, stdout=out, stderr=err)
                except ShellError as e:
                    last_err = e
                    self._alive = False
                    continue
            assert last_err is not None
            raise last_err


# ---- registry ----

_REG: dict[str, HostShell] = {}
_REG_LOCK = threading.Lock()


def get_shell(host_name: str, ssh_target: str, port: int = 22,
              control_path: str | None = None) -> HostShell:
    with _REG_LOCK:
        sh = _REG.get(host_name)
        if sh is None:
            sh = HostShell(host_name, ssh_target, port=port, control_path=control_path)
            _REG[host_name] = sh
        return sh


@atexit.register
def _shutdown_all() -> None:
    for sh in list(_REG.values()):
        try:
            sh.close()
        except Exception:
            pass
    _REG.clear()
