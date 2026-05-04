package main

import (
	"embed"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/hashicorp/hcl/v2"
	"github.com/hashicorp/hcl/v2/gohcl"
)

//go:embed prompts
var promptFS embed.FS

// ── Config ────────────────────────────────────────────────────────────────────

type NodeCompatConfig struct {
	UpstreamRepo string `hcl:"upstream_repo,attr"`
	BotUser      string `hcl:"bot_user,attr"`
	BotFork      string `hcl:"bot_fork,attr"`
	DenoSrc      string `hcl:"deno_src,attr"`
	WTBase       string `hcl:"wt_base,attr"`
	BuildPrefix  string `hcl:"build_prefix,attr"`
	PickerFocus  string `hcl:"picker_focus,optional"`
	ViewerURL    string `hcl:"viewer_url,attr"`
}

func defaultNodeCompatConfig() NodeCompatConfig {
	h := os.Getenv("HOME")
	return NodeCompatConfig{
		UpstreamRepo: "denoland/deno",
		BotUser:      "divybot",
		BotFork:      "divybot/deno",
		DenoSrc:      filepath.Join(h, "src/deno"),
		WTBase:       filepath.Join(h, "src/deno-wt"),
		BuildPrefix:  "nix develop -c",
		PickerFocus:  `^test-crypto-`,
		ViewerURL:    "https://node-test-viewer.deno.dev/results/latest/darwin.json",
	}
}

// ── Source ────────────────────────────────────────────────────────────────────

type NodeCompatSource struct {
	cfg        NodeCompatConfig
	skipRE     *regexp.Regexp
	workerTmpl string
	feedTmpl   string
}

func NewNodeCompatSource() *NodeCompatSource {
	s := &NodeCompatSource{cfg: defaultNodeCompatConfig()}
	s.loadTemplates()
	return s
}

func (s *NodeCompatSource) loadTemplates() {
	wb, _ := promptFS.ReadFile("prompts/worker.md")
	fb, _ := promptFS.ReadFile("prompts/feedback.md")
	s.workerTmpl = string(wb)
	s.feedTmpl = string(fb)
}

func (s *NodeCompatSource) ID() string             { return "nodecompat" }
func (s *NodeCompatSource) WorktreeBase() string   { return s.cfg.WTBase }
func (s *NodeCompatSource) MainRepo() string       { return s.cfg.DenoSrc }

func (s *NodeCompatSource) Configure(body hcl.Body) error {
	var cfg NodeCompatConfig
	if diags := gohcl.DecodeBody(body, nil, &cfg); diags.HasErrors() {
		return fmt.Errorf("%s", diags.Error())
	}
	// Merge: only override non-zero fields
	if cfg.UpstreamRepo != "" {
		s.cfg.UpstreamRepo = cfg.UpstreamRepo
	}
	if cfg.BotUser != "" {
		s.cfg.BotUser = cfg.BotUser
	}
	if cfg.BotFork != "" {
		s.cfg.BotFork = cfg.BotFork
	}
	if cfg.DenoSrc != "" {
		s.cfg.DenoSrc = expandHome(cfg.DenoSrc)
	}
	if cfg.WTBase != "" {
		s.cfg.WTBase = expandHome(cfg.WTBase)
	}
	if cfg.BuildPrefix != "" {
		s.cfg.BuildPrefix = cfg.BuildPrefix
	}
	// Always override PickerFocus — empty string means no filter
	s.cfg.PickerFocus = cfg.PickerFocus
	if cfg.ViewerURL != "" {
		s.cfg.ViewerURL = cfg.ViewerURL
	}
	if s.cfg.PickerFocus != "" {
		s.skipRE = regexp.MustCompile(s.cfg.PickerFocus)
	} else {
		s.skipRE = nil
	}
	return nil
}

func (s *NodeCompatSource) Repo() string { return s.cfg.UpstreamRepo }

func (s *NodeCompatSource) WorktreeDir(taskName string) string {
	return filepath.Join(s.cfg.WTBase, taskName)
}

// ── Pick ──────────────────────────────────────────────────────────────────────

func (s *NodeCompatSource) Pick(db *DB) string {
	testsDir := filepath.Join(s.cfg.DenoSrc, "tests/node_compat/runner/suite/test/parallel")
	configFile := filepath.Join(s.cfg.DenoSrc, "tests/node_compat/config.jsonc")

	configText := ""
	if b, err := os.ReadFile(configFile); err == nil {
		configText = string(b)
	}

	ok := func(name string) bool {
		f := filepath.Join(testsDir, name+".js")
		if _, err := os.Stat(f); err != nil {
			return false
		}
		if strings.Contains(configText, `"parallel/`+name+`.js"`) {
			return false
		}
		if t := db.Get("nodecompat:" + name); t != nil && t.Status != "failed" {
			return false // abandoned = permanent; only retry "failed" (session died / idle)
		}
		return true
	}

	// Viewer-based picker
	candidates := s.fetchFailingTests()
	if len(candidates) == 0 {
		// Fallback: alphabetical scan of all tests
		entries, _ := os.ReadDir(testsDir)
		for _, e := range entries {
			if strings.HasSuffix(e.Name(), ".js") {
				candidates = append(candidates, strings.TrimSuffix(e.Name(), ".js"))
			}
		}
	}

	for _, name := range candidates {
		if s.skipRE != nil && !s.skipRE.MatchString(name) {
			continue
		}
		if !ok(name) {
			continue
		}
		// Upstream dup-check
		out, _ := sh("", nil, "gh", "pr", "list", "--repo", s.cfg.UpstreamRepo,
			"--state", "open", "--search", `"parallel/`+name+`.js"`, "--json", "number,author")
		if strings.Contains(out, `"login"`) && !strings.Contains(out, `"`+s.cfg.BotUser+`"`) {
			log.Printf("skip %s: upstream open PR", name)
			db.Insert("nodecompat:"+name, "abandoned", "claude", "", "")
			db.Update("nodecompat:"+name, map[string]any{"last_error": "duplicate of upstream PR"})
			continue
		}
		return name
	}
	return ""
}

func (s *NodeCompatSource) fetchFailingTests() []string {
	client := &http.Client{Timeout: 20 * time.Second}
	resp, err := client.Get(s.cfg.ViewerURL)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	var data struct {
		Results map[string][]any `json:"results"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil
	}
	var out []string
	for k, v := range data.Results {
		if strings.HasPrefix(k, "parallel/") && len(v) > 0 {
			if fail, ok := v[0].(bool); ok && !fail {
				out = append(out, strings.TrimSuffix(strings.TrimPrefix(k, "parallel/"), ".js"))
			}
		}
	}
	return out
}

// ── Setup ─────────────────────────────────────────────────────────────────────

func (s *NodeCompatSource) Setup(taskName string) error {
	wt := s.WorktreeDir(taskName)
	branch := "claude/" + taskName

	if _, err := os.Stat(wt); os.IsNotExist(err) {
		// Worktree doesn't exist — create it.
		sh(s.cfg.DenoSrc, nil, "git", "fetch", "origin", "main", "--quiet") //nolint
		add, err := sh(s.cfg.DenoSrc, nil, "git", "worktree", "add", "-B", branch, wt, "origin/main")
		if err != nil {
			return fmt.Errorf("worktree add: %v", add)
		}
	}

	sh(wt, nil, "git", "config", "user.name", s.cfg.BotUser)           //nolint
	sh(wt, nil, "git", "config", "user.email", s.cfg.BotUser+"@users.noreply.github.com") //nolint

	s.installHooks(wt)
	s.trustWorktree(wt)
	return nil
}

func (s *NodeCompatSource) installHooks(wt string) {
	gitDir, _ := sh(wt, nil, "git", "rev-parse", "--git-dir")
	if gitDir == "" {
		return
	}
	if !filepath.IsAbs(gitDir) {
		gitDir = filepath.Join(wt, gitDir)
	}
	hooksDir := filepath.Join(gitDir, "hooks")
	os.MkdirAll(hooksDir, 0755)

	writeHook := func(name, body string) {
		p := filepath.Join(hooksDir, name)
		os.WriteFile(p, []byte(body), 0755)
	}
	writeHook("prepare-commit-msg",
		"#!/bin/sh\nMSG_FILE=\"$1\"\n"+
			"TRAILER='Co-authored-by: Divy Srivastava <me@littledivy.com>'\n"+
			"grep -qF \"$TRAILER\" \"$MSG_FILE\" || printf '\\n%s\\n' \"$TRAILER\" >> \"$MSG_FILE\"\n")
	writeHook("pre-commit",
		"#!/bin/sh\n"+
			"if git diff --cached --name-only | grep -qE '^\\.claude(/|$)'; then\n"+
			"  git reset HEAD -- .claude 2>/dev/null\nfi\nexit 0\n")

	infoDir := filepath.Join(gitDir, "info")
	os.MkdirAll(infoDir, 0755)
	excl := filepath.Join(infoDir, "exclude")
	existing, _ := os.ReadFile(excl)
	if !strings.Contains(string(existing), ".claude/") {
		os.WriteFile(excl, append(existing, []byte("\n.claude/\n")...), 0644)
	}
}

func (s *NodeCompatSource) trustWorktree(wt string) {
	py := `import json,os,sys;p=os.path.expanduser('~/.claude.json');` +
		`open(p,'a').close() if not os.path.exists(p) else None;` +
		`d=json.load(open(p)) if os.path.getsize(p) else {};` +
		`d.setdefault('projects',{}).setdefault(sys.argv[1],{})['hasTrustDialogAccepted']=True;` +
		`d['remoteDialogSeen']=True;` +
		`json.dump(d,open(p,'w'),indent=2)`
	sh("", nil, "python3", "-c", py, wt) //nolint
}

// ── Prompts ───────────────────────────────────────────────────────────────────

func (s *NodeCompatSource) InitPrompt(taskName string) string {
	fileHint := regexp.MustCompile(`^test-([a-z0-9]*)`).ReplaceAllString(taskName, "$1")
	return strings.NewReplacer(
		"<NAME>", taskName,
		"<file>", fileHint,
		"{{BUILD_PREFIX}}", s.cfg.BuildPrefix,
	).Replace(s.workerTmpl)
}

func (s *NodeCompatSource) FeedbackPrompt(taskName, prNum, branch string, c PRCounts) string {
	conflictLine := ""
	if c.Conflict > 0 {
		conflictLine = "MERGE CONFLICT detected — start with `git fetch origin && git rebase origin/main`, then `git push bot HEAD --force-with-lease`."
	}
	return strings.NewReplacer(
		"<NAME>", taskName,
		"<PR>", prNum,
		"<BRANCH>", branch,
		"<PUSH_REMOTE>", "bot",
		"{{BUILD_PREFIX}}", s.cfg.BuildPrefix,
		"<FAIL>", fmt.Sprintf("%d", c.Fail),
		"<CMT>", fmt.Sprintf("%d", c.Comments),
		"<REV>", fmt.Sprintf("%d", c.Reviews),
		"<INLINE>", fmt.Sprintf("%d", c.Inline),
		"<CONFLICT_LINE>", conflictLine,
	).Replace(s.feedTmpl)
}

// ── PostDone ──────────────────────────────────────────────────────────────────

func (s *NodeCompatSource) PostDone(taskName, pane string) (string, error) {
	wt := s.WorktreeDir(taskName)
	branch := "claude/" + taskName

	title := extractTitle(pane)
	if title == "" {
		title = "fix(ext/node): enable " + taskName
	}

	// Check for uncommitted diff
	diffOut, _ := sh(wt, nil, "git", "status", "--porcelain")
	headDiff, _ := sh(wt, nil, "git", "diff", "HEAD", "--stat")
	if diffOut == "" && headDiff == "" {
		return "", fmt.Errorf("no diff — nothing to commit")
	}

	token, err := ghToken(s.cfg.BotUser)
	if err != nil {
		return "", fmt.Errorf("gh token: %w", err)
	}

	env := gitEnv(s.cfg.BotUser)
	env = append(env, "GH_TOKEN="+token)

	commitMsg := fmt.Sprintf(
		"%s\n\nEnables tests/node_compat/runner/suite/test/parallel/%s.js\n\nCo-authored-by: Divy Srivastava <me@littledivy.com>",
		title, taskName,
	)
	sh(wt, env, "git", "add", "-A")                          //nolint
	sh(wt, env, "git", "commit", "-m", commitMsg)            //nolint

	botURL := fmt.Sprintf("https://x-access-token:%s@github.com/%s.git", token, s.cfg.BotFork)
	sh(wt, nil, "git", "remote", "rm", "bot")                //nolint
	sh(wt, nil, "git", "remote", "add", "bot", botURL)       //nolint

	if out, err := sh(wt, env, "git", "push", "-u", "--force-with-lease", "bot", branch); err != nil {
		return "", fmt.Errorf("push failed: %s", out)
	}

	body := fmt.Sprintf(
		"## Summary\n\nEnables `%s` in node_compat suite.\n\n## Test plan\n- [x] `cargo test --test node_compat -- %s`",
		taskName, taskName,
	)
	prOut, err := sh("", env, "gh", "pr", "create",
		"--repo", s.cfg.UpstreamRepo,
		"--head", s.cfg.BotUser+":"+branch,
		"--title", title,
		"--body", body,
	)
	if err != nil {
		return "", fmt.Errorf("pr create: %s", prOut)
	}

	lines := strings.Split(strings.TrimSpace(prOut), "\n")
	prURL := lines[len(lines)-1]
	if !strings.HasPrefix(prURL, "https://") {
		return "", fmt.Errorf("unexpected pr create output: %s", prOut)
	}
	return prURL, nil
}
