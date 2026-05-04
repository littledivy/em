package main

import (
	"context"
	"crypto/rand"
	"crypto/sha1"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// ── Paths ─────────────────────────────────────────────────────────────────────

var (
	root    = expandHome(envOr("DIVYBOT_ROOT", "~/.divybot"))
	inbox   = filepath.Join(root, "inbox")
	haltF   = filepath.Join(root, "halt")
	cfgPath = filepath.Join(root, "config.hcl")
	dbPath  = filepath.Join(root, "tasks.db")
)

// ── Daemon ────────────────────────────────────────────────────────────────────

type Daemon struct {
	db      *DB
	cfg     *Config
	sources map[string]Source
}

func (d *Daemon) sourceFor(taskID string) Source {
	name, _, _ := strings.Cut(taskID, ":")
	return d.sources[name]
}

func taskName(taskID string) string {
	_, name, found := strings.Cut(taskID, ":")
	if !found {
		return taskID
	}
	return name
}

// ── Entry point ───────────────────────────────────────────────────────────────

func main() {
	log.SetFlags(log.Ltime)
	os.Setenv("PATH", "/opt/homebrew/bin:"+os.Getenv("PATH"))

	for _, d := range []string{root, inbox} {
		os.MkdirAll(d, 0755)
	}

	initTmux()

	srcs := map[string]Source{
		"nodecompat": NewNodeCompatSource(),
	}
	cfg := loadConfig(cfgPath, srcs)
	db := mustOpenDB(dbPath)
	defer db.Close()

	d := &Daemon{db: db, cfg: cfg, sources: srcs}

	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "status":
			db.PrintStatus()
			return
		case "spawn":
			if len(os.Args) < 3 {
				fmt.Fprintln(os.Stderr, "usage: divybot spawn <sourceID:taskName>")
				os.Exit(1)
			}
			taskID := os.Args[2]
			if !strings.Contains(taskID, ":") {
				taskID = "nodecompat:" + taskID
			}
			if err := d.spawnWorker(taskID); err != nil {
				log.Fatalf("spawn: %v", err)
			}
			return
		case "halt":
			os.WriteFile(haltF, []byte("halted"), 0644)
			fmt.Println("halted")
			return
		case "resume":
			os.Remove(haltF)
			fmt.Println("resumed")
			return
		default:
			fmt.Fprintf(os.Stderr, "usage: divybot [status|spawn|halt|resume]\n")
			os.Exit(1)
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	slots := make(chan struct{}, 4)
	trigger := func() {
		select {
		case slots <- struct{}{}:
		default:
		}
	}

	var wg sync.WaitGroup
	wg.Add(4)
	go d.runWorkerPoller(ctx, &wg, trigger)
	go d.runPRMonitor(ctx, &wg)
	go d.runSpawner(ctx, &wg, slots)
	go d.runInboxDeliverer(ctx, &wg)
	log.Printf("divybot started (root=%s)", root)
	wg.Wait()
}

// ── Goroutines ────────────────────────────────────────────────────────────────

func (d *Daemon) runWorkerPoller(ctx context.Context, wg *sync.WaitGroup, trigger func()) {
	defer wg.Done()
	tick := time.NewTicker(d.cfg.WorkerPoll())
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			d.pollWorkers(trigger)
		}
	}
}

func (d *Daemon) runPRMonitor(ctx context.Context, wg *sync.WaitGroup) {
	defer wg.Done()
	tick := time.NewTicker(d.cfg.PRPoll())
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			d.pollPRs()
		}
	}
}

func (d *Daemon) runSpawner(ctx context.Context, wg *sync.WaitGroup, slots <-chan struct{}) {
	defer wg.Done()
	tick := time.NewTicker(30 * time.Second)
	defer tick.Stop()
	trySpawn := func() {
		if _, err := os.Stat(haltF); err == nil {
			return
		}
		if d.db.TotalOpen() >= d.cfg.Timing.OpenPRCap {
			log.Printf("open PR cap (%d) reached", d.cfg.Timing.OpenPRCap)
			return
		}
		running := d.db.RunningCounts()
		total := 0
		for _, n := range running {
			total += n
		}
		if total >= d.cfg.TotalCapacity() {
			return
		}
		for _, src := range d.sources {
			task := src.Pick(d.db)
			if task == "" {
				continue
			}
			taskID := src.ID() + ":" + task
			if err := d.spawnWorker(taskID); err != nil {
				log.Printf("spawn %s: %v", taskID, err)
			}
			return
		}
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-slots:
			trySpawn()
		case <-tick.C:
			trySpawn()
		}
	}
}

func (d *Daemon) runInboxDeliverer(ctx context.Context, wg *sync.WaitGroup) {
	defer wg.Done()
	tick := time.NewTicker(10 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			d.deliverInbox()
		}
	}
}

// ── Worker polling ────────────────────────────────────────────────────────────

func (d *Daemon) pollWorkers(trigger func()) {
	for _, t := range d.db.WithStatus("running") {
		session := sessionFor(t.ID)

		if !tmuxHasSession(session) {
			if t.PRURL != "" {
				log.Printf("dead session (PR exists) → review: %s", t.ID)
				d.db.Update(t.ID, map[string]any{"status": "review", "last_pr_hash": ""})
			} else {
				log.Printf("dead session → failed: %s", t.ID)
				d.db.Update(t.ID, map[string]any{"status": "failed", "last_error": "session died"})
			}
			trigger()
			continue
		}

		pane := tmuxCapture(session, 500)

		switch {
		case detectNoAction(pane):
			log.Printf("no-action → review: %s", t.ID)
			d.db.Update(t.ID, map[string]any{"status": "review", "last_error": "no-action"})
			tmuxKill(session)
			trigger()

		case detectDone(pane):
			log.Printf("DONE: %s", t.ID)
			d.handleDone(t, pane)
			trigger()

		case detectEscalate(pane):
			reason := tailLines(pane, 5)
			log.Printf("ESCALATE: %s\n%s", t.ID, reason)
			d.db.Update(t.ID, map[string]any{"status": "abandoned", "last_error": "escalated"})
			tmuxKill(session)
			// no trigger — let 30s tick spawn next; avoids storm when many tasks escalate rapidly

		default:
			h := hashLines(pane, 50)
			if h == t.LastHash {
				idle := t.IdleTicks + 1
				if idle >= d.cfg.Timing.IdleTicksCap {
					tmuxKill(session)
					if t.PRURL != "" {
						log.Printf("idle → review: %s", t.ID)
						d.db.Update(t.ID, map[string]any{"status": "review", "last_pr_hash": "", "idle_ticks": 0})
					} else {
						log.Printf("idle → failed: %s", t.ID)
						d.db.Update(t.ID, map[string]any{"status": "failed", "last_error": "idle timeout", "idle_ticks": 0})
					}
					trigger()
				} else {
					d.db.Update(t.ID, map[string]any{"idle_ticks": idle})
					log.Printf("thinking (%d/%d): %s", idle, d.cfg.Timing.IdleTicksCap, t.ID)
				}
			} else {
				d.db.Update(t.ID, map[string]any{"last_hash": h, "idle_ticks": 0})
				log.Printf("active: %s", t.ID)
			}
		}
	}
}

func (d *Daemon) handleDone(t Task, pane string) {
	src := d.sourceFor(t.ID)
	if src == nil {
		return
	}
	prURL, err := src.PostDone(taskName(t.ID), pane)
	tmuxKill(sessionFor(t.ID))
	if err != nil {
		log.Printf("postDone %s: %v", t.ID, err)
		d.db.Update(t.ID, map[string]any{"status": "failed", "last_error": err.Error()})
		return
	}
	d.db.Update(t.ID, map[string]any{
		"status":      "review",
		"pr_url":      prURL,
		"last_pr_hash": "",
		"session_id":  "",
	})
	log.Printf("PR opened: %s → %s", t.ID, prURL)
}

// ── PR monitoring ─────────────────────────────────────────────────────────────

func (d *Daemon) pollPRs() {
	for _, t := range d.db.WithStatuses("review", "running") {
		if t.PRURL == "" {
			continue
		}
		src := d.sourceFor(t.ID)
		if src == nil {
			continue
		}

		prNum := t.PRURL[strings.LastIndex(t.PRURL, "/")+1:]
		sig := fetchPRSignal(prNum, src.Repo())

		switch sig.State {
		case "MERGED":
			d.db.Update(t.ID, map[string]any{"status": "merged"})
			log.Printf("merged: %s", t.ID)
			continue
		case "CLOSED":
			d.db.Update(t.ID, map[string]any{"status": "abandoned", "last_error": "closed"})
			log.Printf("closed: %s", t.ID)
			continue
		}

		if sig.Hash == t.LastPRHash {
			if t.Status == "review" && sig.Counts.Fail+sig.Counts.Comments+sig.Counts.Reviews+sig.Counts.Inline == 0 {
				log.Printf("no-signal: %s", t.ID)
			}
			continue
		}

		// Feedback cooldown
		if t.LastFeedbackAt > 0 {
			elapsed := time.Since(time.Unix(t.LastFeedbackAt, 0))
			if elapsed < d.cfg.FeedbackCooldown() {
				log.Printf("cooldown (%v remain): %s", (d.cfg.FeedbackCooldown() - elapsed).Round(time.Second), t.ID)
				continue
			}
		}

		d.db.Update(t.ID, map[string]any{"last_pr_hash": sig.Hash})

		// Baseline: first hash, nothing actionable → just store
		if t.LastPRHash == "" && sig.Counts.Fail == 0 && sig.Counts.Comments == 0 &&
			sig.Counts.Reviews == 0 && sig.Counts.Inline == 0 && sig.Counts.Conflict == 0 {
			log.Printf("baseline clean: %s", t.ID)
			continue
		}

		// Live worker: paste an update instead of respawning
		if t.Status == "running" {
			session := sessionFor(t.ID)
			if tmuxHasSession(session) {
				msg := fmt.Sprintf(
					"PR #%s has new activity: fail=%d cmt=%d rev=%d inline=%d. Address alongside current work, then `<<NODE_BOT_DONE>> <summary>`.",
					prNum, sig.Counts.Fail, sig.Counts.Comments, sig.Counts.Reviews, sig.Counts.Inline,
				)
				tmuxPaste(session, msg)
				d.db.Update(t.ID, map[string]any{"last_feedback_at": time.Now().Unix()})
				log.Printf("pasted update to live worker: %s", t.ID)
				continue
			}
		}

		d.respawnForFeedback(t, prNum, sig.Counts)
	}
}

// ── Spawn & respawn ───────────────────────────────────────────────────────────

func (d *Daemon) spawnWorker(taskID string) error {
	src := d.sourceFor(taskID)
	if src == nil {
		return fmt.Errorf("no source for %s", taskID)
	}
	name := taskName(taskID)

	running := d.db.RunningCounts()
	cli, ok := d.cfg.pickCLI(running)
	if !ok {
		return fmt.Errorf("no capacity")
	}
	adapter, ok := cliRegistry[cli]
	if !ok {
		return fmt.Errorf("unknown cli %s", cli)
	}

	if err := src.Setup(name); err != nil {
		return fmt.Errorf("setup: %w", err)
	}

	sid := newUUID()
	session := sessionFor(taskID)
	branch := "claude/" + name
	wt := src.WorktreeDir(name)

	tmuxNewSession(session, wt)
	for _, key := range adapter.PrePromptKeys() {
		tmuxSendLine(session, key)
		time.Sleep(2 * time.Second)
	}
	tmuxSendLine(session, adapter.Launch(sid, name))
	time.Sleep(2 * time.Second)

	tmuxPaste(session, src.InitPrompt(name))
	d.db.Insert(taskID, "running", cli, sid, branch)
	log.Printf("spawned: %s via %s (sid=%s)", taskID, cli, sid[:8])
	return nil
}

func (d *Daemon) respawnForFeedback(t Task, prNum string, counts PRCounts) {
	src := d.sourceFor(t.ID)
	if src == nil {
		return
	}
	name := taskName(t.ID)
	wt := src.WorktreeDir(name)

	if _, err := os.Stat(wt); err != nil {
		log.Printf("worktree gone, can't respawn: %s", t.ID)
		return
	}

	session := sessionFor(t.ID)
	sid := t.SessionID
	cli, ok := cliRegistry[t.CLI]
	if !ok {
		cli = cliRegistry["claude"]
	}

	if !tmuxHasSession(session) {
		var inner string
		if sid != "" {
			inner = cli.Resume(sid, name)
		} else {
			sid = newUUID()
			inner = cli.Launch(sid, name)
			d.db.Update(t.ID, map[string]any{"session_id": sid})
		}
		tmuxNewSession(session, wt)
		tmuxSendLine(session, inner)
		time.Sleep(2 * time.Second)
		tmuxClearHistory(session)
	}

	prompt := src.FeedbackPrompt(name, prNum, t.Branch, counts)
	tmuxPaste(session, prompt)
	d.db.Update(t.ID, map[string]any{
		"status":          "running",
		"attempts":        t.Attempts + 1,
		"last_feedback_at": time.Now().Unix(),
	})
	log.Printf("feedback respawn: %s (#%s) fail=%d cmt=%d rev=%d inline=%d",
		t.ID, prNum, counts.Fail, counts.Comments, counts.Reviews, counts.Inline)
}

// ── Inbox delivery ────────────────────────────────────────────────────────────

func (d *Daemon) deliverInbox() {
	entries, _ := os.ReadDir(inbox)
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".txt") {
			continue
		}
		// Filename is taskName (without source prefix). Try all sources.
		baseName := strings.TrimSuffix(e.Name(), ".txt")
		for _, src := range d.sources {
			taskID := src.ID() + ":" + baseName
			t := d.db.Get(taskID)
			if t == nil || t.Status != "running" {
				continue
			}
			session := sessionFor(taskID)
			if !tmuxHasSession(session) {
				continue
			}
			msg, _ := os.ReadFile(filepath.Join(inbox, e.Name()))
			tmuxPaste(session, string(msg))
			os.Remove(filepath.Join(inbox, e.Name()))
			d.db.Update(taskID, map[string]any{"idle_ticks": 0})
			log.Printf("delivered inbox: %s", taskID)
			break
		}
	}
}

// ── PR signal ─────────────────────────────────────────────────────────────────

type prSignal struct {
	Hash   string
	State  string
	Counts PRCounts
}

func fetchPRSignal(prNum, repo string) prSignal {
	type checkRun struct {
		Conclusion string `json:"conclusion"`
		Status     string `json:"status"`
		State      string `json:"state"`
	}
	type author struct{ Login string `json:"login"` }
	type comment struct {
		Body   string `json:"body"`
		Author author `json:"author"`
	}
	type review struct {
		State  string `json:"state"`
		Body   string `json:"body"`
		Author author `json:"author"`
	}
	type prData struct {
		State             string      `json:"state"`
		Mergeable         string      `json:"mergeable"`
		MergeStateStatus  string      `json:"mergeStateStatus"`
		StatusCheckRollup []checkRun  `json:"statusCheckRollup"`
		Comments          []comment   `json:"comments"`
		Reviews           []review    `json:"reviews"`
	}

	out, err := sh("", nil, "gh", "pr", "view", prNum, "--repo", repo, "--json",
		"state,statusCheckRollup,reviews,comments,mergeable,mergeStateStatus")
	if err != nil {
		return prSignal{}
	}

	var pr prData
	if err := json.Unmarshal([]byte(out), &pr); err != nil {
		return prSignal{}
	}

	type inlineUser struct{ Login string `json:"login"` }
	type inlineComment struct {
		Body string     `json:"body"`
		User inlineUser `json:"user"`
	}
	inlineOut, _ := sh("", nil, "gh", "api", fmt.Sprintf("repos/%s/pulls/%s/comments", repo, prNum))
	var inline []inlineComment
	json.Unmarshal([]byte(inlineOut), &inline) //nolint

	c := PRCounts{
		Conflict: boolInt(pr.Mergeable == "CONFLICTING" || pr.MergeStateStatus == "DIRTY"),
	}
	for _, ch := range pr.StatusCheckRollup {
		if ch.Conclusion == "FAILURE" {
			c.Fail++
		}
	}
	for _, cm := range pr.Comments {
		if !reBotLogin.MatchString(cm.Author.Login) {
			c.Comments++
		}
	}
	for _, rv := range pr.Reviews {
		if !reBotLogin.MatchString(rv.Author.Login) {
			c.Reviews++
		}
	}
	for _, ic := range inline {
		if !reBotLogin.MatchString(ic.User.Login) {
			c.Inline++
		}
	}

	// Hash for change detection — includes text so new comments fire even at same count
	type sigKey struct {
		State    string
		Counts   PRCounts
		Comments []string
		Reviews  []string
		Inline   []string
	}
	sk := sigKey{State: pr.State, Counts: c}
	for _, cm := range pr.Comments {
		if !reBotLogin.MatchString(cm.Author.Login) {
			sk.Comments = append(sk.Comments, cm.Body)
		}
	}
	for _, rv := range pr.Reviews {
		if !reBotLogin.MatchString(rv.Author.Login) {
			sk.Reviews = append(sk.Reviews, rv.State+rv.Body)
		}
	}
	for _, ic := range inline {
		if !reBotLogin.MatchString(ic.User.Login) {
			sk.Inline = append(sk.Inline, ic.Body)
		}
	}
	b, _ := json.Marshal(sk)
	h := sha1.Sum(b)

	return prSignal{
		Hash:   fmt.Sprintf("%x", h),
		State:  pr.State,
		Counts: c,
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

// sh runs a command, returns (stdout, error). dir="" = inherit cwd. env=nil = inherit.
func sh(dir string, env []string, args ...string) (string, error) {
	cmd := exec.Command(args[0], args[1:]...)
	if dir != "" {
		cmd.Dir = dir
	}
	if env != nil {
		cmd.Env = append(os.Environ(), env...)
	}
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		se := strings.TrimSpace(stderr.String())
		if se != "" {
			return strings.TrimSpace(string(out)), fmt.Errorf("%w: %s", err, se)
		}
		return strings.TrimSpace(string(out)), err
	}
	return strings.TrimSpace(string(out)), nil
}

func ghToken(user string) (string, error) {
	out, err := sh("", nil, "gh", "auth", "token", "--user", user)
	if err != nil || out == "" {
		return "", fmt.Errorf("no gh auth for user %s", user)
	}
	return out, nil
}

func gitEnv(user string) []string {
	return []string{
		"GIT_AUTHOR_NAME=" + user,
		"GIT_AUTHOR_EMAIL=" + user + "@users.noreply.github.com",
		"GIT_COMMITTER_NAME=" + user,
		"GIT_COMMITTER_EMAIL=" + user + "@users.noreply.github.com",
	}
}

func newUUID() string {
	b := make([]byte, 16)
	rand.Read(b) //nolint
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

func expandHome(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") {
		return filepath.Join(os.Getenv("HOME"), p[1:])
	}
	return p
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
