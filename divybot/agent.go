package main

import (
	"fmt"
	"regexp"
	"strings"
)

// ── Source plugin interface ───────────────────────────────────────────────────

// PRCounts holds feedback signal counts from a PR.
type PRCounts struct {
	Fail, Comments, Reviews, Inline, Conflict int
}

// Source is a plugin that provides tasks to the orchestrator.
// Register sources in main() via RegisterSource.
// Task IDs in the DB are "<sourceID>:<taskName>" e.g. "nodecompat:test-crypto-hash".
type Source interface {
	ID() string
	Pick(db *DB) string // "" = nothing to do right now
	Setup(taskName string) error
	WorktreeDir(taskName string) string
	Repo() string // GitHub repo for PR polling e.g. "denoland/deno"
	InitPrompt(taskName string) string
	FeedbackPrompt(taskName, prNum, branch string, c PRCounts) string
	PostDone(taskName, pane string) (prURL string, err error)
}

// ── CLI adapters ──────────────────────────────────────────────────────────────

type CLI interface {
	Name() string
	Launch(sid, task string) string
	Resume(sid, task string) string
	PrePromptKeys() []string
}

type claudeCLI struct{}

func (claudeCLI) Name() string { return "claude" }
func (claudeCLI) Launch(sid, task string) string {
	return fmt.Sprintf(
		"claude --session-id %s --permission-mode bypassPermissions --model sonnet -n 'deno-bot:%s'",
		sid, task,
	)
}
func (claudeCLI) Resume(sid, task string) string {
	return fmt.Sprintf(
		"claude --resume %s --permission-mode bypassPermissions --model sonnet -n 'deno-bot:%s'",
		sid, task,
	)
}
func (claudeCLI) PrePromptKeys() []string { return nil }

type codexCLI struct{}

func (codexCLI) Name() string { return "codex" }
func (codexCLI) Launch(_, _ string) string {
	return `codex -c model="gpt-5.4" --dangerously-bypass-approvals-and-sandbox`
}
func (codexCLI) Resume(_, _ string) string {
	return `codex resume --last -c model="gpt-5.4" --dangerously-bypass-approvals-and-sandbox`
}
func (codexCLI) PrePromptKeys() []string { return []string{"1"} }

type geminiCLI struct{}

func (geminiCLI) Name() string                 { return "gemini" }
func (geminiCLI) Launch(_, _ string) string    { return "gemini -y" }
func (geminiCLI) Resume(_, _ string) string    { return "gemini -y -r latest" }
func (geminiCLI) PrePromptKeys() []string      { return nil }

var cliRegistry = map[string]CLI{
	"claude": claudeCLI{},
	"codex":  codexCLI{},
	"gemini": geminiCLI{},
}

// ── Sentinel detection ────────────────────────────────────────────────────────

var (
	reDone      = regexp.MustCompile("(?:^|[^`])(?:<<NODE_BOT_DONE>>|<>)\\s+\\S{3,}")
	reDoneTitle = regexp.MustCompile("(?:<<NODE_BOT_DONE>>|<>)\\s+(.+?)\\s*$")
	reEscalate  = regexp.MustCompile("(?:<<NODE_BOT_ESCALATE>>|<>).*(?:duplicate|requires|cannot|impossible|unsupported|blocked|escalate|stuck)")
	reNoAction  = regexp.MustCompile("(?:<>|<<NODE_BOT_DONE>>).*(?:already|flaky|unrelated|no actionable|no action|nothing to fix|moot|no code change)")
	reBotLogin  = regexp.MustCompile(`\[bot\]$|^CLAassistant$|^github-actions$|^codecov$|^vercel$|^renovate$|^dependabot$|^divybot$`)
)

func tailLines(pane string, n int) string {
	lines := strings.Split(pane, "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

func detectDone(pane string) bool     { return reDone.MatchString(tailLines(pane, 80)) }
func detectEscalate(pane string) bool { return reEscalate.MatchString(tailLines(pane, 80)) }
func detectNoAction(pane string) bool { return reNoAction.MatchString(tailLines(pane, 20)) }

func extractTitle(pane string) string {
	var last string
	for _, line := range strings.Split(tailLines(pane, 80), "\n") {
		if m := reDoneTitle.FindStringSubmatch(line); m != nil {
			t := strings.TrimRight(m[1], ".")
			if len(t) > 8 {
				last = t
			}
		}
	}
	if len(last) > 90 {
		last = last[:90]
	}
	return last
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
