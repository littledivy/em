"""Persistent ssh-bash shell per remote host.

Replaces "ssh-per-command" with one long-lived ssh that pipes a login bash.
Commands flow as `(cmd) >out 2>err; echo SENTINELS` framed with random uuids
so the reader can split stdout / stderr / rc deterministically.

Why: russh on the bot VMs costs ~4s per channel-open. Re-using a single
channel drops per-command overhead to ~50ms.

Robustness:
- The shell respawns on death (EOF, timeout, broken pipe).
- A command that times out kills the shell so the next call gets a clean one.
- Failed sends retry once on a fresh shell before raising.
- Output never collides with sentinels: each command gets its own uuid.

Caveats:
- Binary stdout works because we read line-by-line and join — but a sentinel
  string appearing verbatim in command output (with newlines around it) would
  fool the parser. The uuid suffix makes that effectively impossible.
- Commands are run inside a single bash; PWD/exports persist between calls
  unless callers reset. We always pass full cwd/env via the wrapper so no
  caller has to care.
"""
from __future__ import annotations

import atexit
import os
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass


@dataclass
class ShellResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class ShellError(RuntimeError):
    pass


class HostShell:
    """One-per-host long-lived ssh+bash. Thread-safe via a single lock; in
    practice tick.py is single-threaded but the lock keeps this re-usable.
    """

    def __init__(self, ssh_target: str, port: int = 22, control_path: str | None = None) -> None:
        self.ssh_target = ssh_target
        self.port = port
        self.control_path = control_path
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ---- lifecycle ----

    def _spawn(self) -> None:
        ssh_args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-p", str(self.port),
        ]
        if self.control_path:
            ssh_args += ["-o", "ControlMaster=auto",
                         "-o", f"ControlPath={self.control_path}",
                         "-o", "ControlPersist=30m"]
        # `bash -l` for login PATH (claude/codex/gemini/tmux/sccache/gh/cargo).
        # Force PS1='' and disable echo just in case.
        ssh_args += [self.ssh_target, "bash -l"]

        self.proc = subprocess.Popen(
            ssh_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merged so we don't lose ssh-side errors
            bufsize=0,
            text=True,
            env={**os.environ},
        )
        # Drain any login banner / MOTD before first sentinel command.
        # We just queue an immediate no-op and let `_run_locked` consume.
        self._send_command("true", warmup=True)

    def _ensure_alive(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = None
            self._spawn()

    def close(self) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.write("exit\n")
                    self.proc.stdin.flush()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    self.proc.kill()
            self.proc = None

    # ---- send/recv ----

    def _send_command(self, raw_bash: str, warmup: bool = False) -> tuple[str, str, int]:
        """Write a framed command. Returns (stdout, stderr, rc)."""
        assert self.proc is not None
        uid = uuid.uuid4().hex[:16]
        BEGIN = f"__BEGIN_{uid}__"
        SEP = f"__SEP_{uid}__"
        END = f"__END_{uid}__"
        # \r-tolerance: we strip both \r and \n in matching.
        wrapped = (
            "( "
            f"{raw_bash}"
            " ) > /tmp/.hs_out_$$ 2> /tmp/.hs_err_$$\n"
            "_RC=$?\n"
            f"printf '\\n{BEGIN}\\n'\n"
            "cat /tmp/.hs_out_$$ 2>/dev/null\n"
            f"printf '\\n{SEP}\\n'\n"
            "cat /tmp/.hs_err_$$ 2>/dev/null\n"
            f"printf '\\n{END}_%s\\n' \"$_RC\"\n"
            "rm -f /tmp/.hs_out_$$ /tmp/.hs_err_$$\n"
        )
        try:
            self.proc.stdin.write(wrapped)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self.proc = None
            raise ShellError(f"shell stdin closed: {e}")

        return self._read_until_sentinels(uid, warmup=warmup)

    def _read_until_sentinels(self, uid: str, warmup: bool, timeout: float = 300.0) -> tuple[str, str, int]:
        BEGIN = f"__BEGIN_{uid}__"
        SEP = f"__SEP_{uid}__"
        END = f"__END_{uid}_"  # rc appended; we match prefix
        deadline = time.time() + timeout
        out_lines: list[str] = []
        err_lines: list[str] = []
        mode = "pre"  # pre -> out -> err -> done

        assert self.proc is not None
        while True:
            if time.time() > deadline:
                self._die(f"timeout waiting for {uid}")
                raise ShellError(f"command timed out (uid {uid})")
            line = self.proc.stdout.readline()
            if not line:
                self._die("shell EOF")
                raise ShellError("shell died mid-command")
            stripped = line.rstrip("\r\n")
            if mode == "pre":
                if stripped == BEGIN:
                    mode = "out"
                # else: drop login banner + any stray output
                continue
            if mode == "out":
                if stripped == SEP:
                    mode = "err"
                    continue
                out_lines.append(line)
                continue
            if mode == "err":
                if stripped.startswith(END):
                    rc_str = stripped[len(END):]
                    try:
                        rc = int(rc_str)
                    except ValueError:
                        rc = 1
                    out = "".join(out_lines)
                    if out.endswith("\n"):
                        out = out[:-1]
                    err = "".join(err_lines)
                    if err.endswith("\n"):
                        err = err[:-1]
                    return out, err, rc
                err_lines.append(line)

    def _die(self, why: str) -> None:
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    # ---- public API ----

    def run(self, argv: list[str], cwd: str | None = None,
            env: dict[str, str] | None = None,
            timeout: float = 300.0) -> ShellResult:
        """Run argv on the host. Quotes args; sets cwd via `cd && ` and env via
        env-var prefix. Raises ShellError on shell death; returns ShellResult
        with the command's rc otherwise."""
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
                    out, err, rc = self._send_command(bash)
                    return ShellResult(args=argv, returncode=rc, stdout=out, stderr=err)
                except ShellError as e:
                    last_err = e
                    self.proc = None  # force respawn next iter
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
            sh = HostShell(ssh_target, port=port, control_path=control_path)
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
