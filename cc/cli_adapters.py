"""Per-CLI launch/resume adapters.

Workers can run under any CLI that produces the shared sentinel format
(<<NODE_BOT_DONE>>, <<NODE_BOT_ESCALATE>>). Adapters only differ in the
shell line we type into a tmux pane to start (or resume) the agent.

The pane's cwd is the worktree, set when tmux new-session -c <wt>. Adapters
should NOT cd themselves; just launch the agent.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CliAdapter:
    name: str
    bin: str  # binary name (resolved via PATH on the executing host)

    # ---- subclass overrides ----
    def launch(self, sid: str, task: str) -> str:
        raise NotImplementedError

    def resume(self, sid: str, task: str) -> str | None:
        """Shell line to resume a prior session, or None if cli has no resume."""
        return None

    def supports_remote_control(self) -> bool:
        return False

    def pre_prompt_keys(self) -> list[str]:
        """Lines/keys to send between launch and prompt-paste — answers any
        startup prompts (e.g. codex's 'Do you trust this directory?'). Default
        none; subclasses override."""
        return []


class ClaudeAdapter(CliAdapter):
    def launch(self, sid: str, task: str) -> str:
        # Pin new spawns to sonnet (operator preference — cheaper than opus,
        # fast enough for these node-compat fixes). Use the `sonnet` alias
        # rather than a versioned id — the CLI rejects non-existent ids
        # (we hit this with `claude-sonnet-4-7` which isn't a real model).
        # Resume deliberately doesn't pin a model so existing sessions keep
        # whatever they started with.
        return (
            f"{self.bin} --session-id {sid} --permission-mode bypassPermissions "
            f"--model sonnet "
            f"-n 'deno-bot:{task}'"
        )

    def resume(self, sid: str, task: str) -> str | None:
        return (
            f"{self.bin} --resume {sid} --permission-mode bypassPermissions "
            f"-n 'deno-bot:{task}'"
        )

    def supports_remote_control(self) -> bool:
        return True


class CodexAdapter(CliAdapter):
    """OpenAI codex CLI. Resume uses --last (most recent codex session in
    that worktree's cwd). sid is unused but stored for db consistency."""

    def launch(self, sid: str, task: str) -> str:
        return f'{self.bin} -c model="gpt-5.4" --dangerously-bypass-approvals-and-sandbox'

    def resume(self, sid: str, task: str) -> str | None:
        return f'{self.bin} resume --last -c model="gpt-5.4" --dangerously-bypass-approvals-and-sandbox'

    def pre_prompt_keys(self) -> list[str]:
        # Codex shows "Do you trust this directory?" on first start in a new dir
        # (worker worktrees are always fresh). Answer "1" (Yes, continue).
        return ["1"]


class GeminiAdapter(CliAdapter):
    """Google gemini-cli. -y is yolo (auto-approve). resume 'latest' picks
    most recent session for current project."""

    def launch(self, sid: str, task: str) -> str:
        return f"{self.bin} -y"

    def resume(self, sid: str, task: str) -> str | None:
        return f"{self.bin} -y -r latest"


ADAPTERS: dict[str, CliAdapter] = {
    "claude": ClaudeAdapter(name="claude", bin="claude"),
    "codex":  CodexAdapter(name="codex",  bin="codex"),
    "gemini": GeminiAdapter(name="gemini", bin="gemini"),
}


def adapter_for(cli: str) -> CliAdapter:
    if cli not in ADAPTERS:
        raise ValueError(f"unknown cli: {cli}")
    return ADAPTERS[cli]
