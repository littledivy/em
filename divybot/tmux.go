package main

import (
	"crypto/sha1"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var (
	tmuxBin    string
	socketDir  string
	socket     string
	reNonAlnum = regexp.MustCompile(`[^A-Za-z0-9-]`)
)

func initTmux() {
	tmuxBin = "/opt/homebrew/bin/tmux"
	if _, err := os.Stat(tmuxBin); err != nil {
		tmuxBin = "tmux" // fallback to PATH
	}
	// Use /tmp (not $TMPDIR) — stable path, survives re-login, viewer can hard-code it.
	socketDir = "/tmp/divybot-sockets"
	socket = filepath.Join(socketDir, "divybot.sock")
	os.MkdirAll(socketDir, 0755)
	// Ensure tmux server is running so setenv works.
	exec.Command(tmuxBin, "-S", socket, "start-server").Run() //nolint
	pushSccacheEnv()
}

// pushSccacheEnv sets RUSTC_WRAPPER=sccache and related vars as tmux global
// env so every worker pane inherits them without touching worktree files.
func pushSccacheEnv() {
	sccacheBin := "/opt/homebrew/bin/sccache"
	if _, err := os.Stat(sccacheBin); err != nil {
		sccacheBin = "sccache"
	}

	set := func(k, v string) { tmuxSilent("setenv", "-g", k, v) }

	// Load S3/MinIO creds from sccache.env if present.
	envFile := expandHome("~/.divybot/sccache.env")
	if b, err := os.ReadFile(envFile); err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			line = strings.TrimPrefix(line, "export ")
			k, v, ok := strings.Cut(line, "=")
			if ok {
				set(strings.TrimSpace(k), strings.Trim(strings.TrimSpace(v), `"`))
			}
		}
	}

	set("RUSTC_WRAPPER", sccacheBin)
	set("SCCACHE_DIR", expandHome("~/.cache/sccache"))
	set("SCCACHE_CACHE_SIZE", "60G")
	set("CARGO_NET_OFFLINE", "true")
	log.Printf("sccache env pushed (wrapper=%s)", sccacheBin)
}

func tmux(args ...string) (string, error) {
	cmd := exec.Command(tmuxBin, append([]string{"-S", socket}, args...)...)
	out, err := cmd.Output()
	return strings.TrimSpace(string(out)), err
}

func tmuxSilent(args ...string) {
	if _, err := tmux(args...); err != nil {
		log.Printf("tmux %v: %v", args[:min(len(args), 3)], err)
	}
}

func tmuxHasSession(name string) bool {
	_, err := tmux("has-session", "-t", name)
	return err == nil
}

func tmuxKill(name string) {
	tmux("kill-session", "-t", name) //nolint
}

func tmuxNewSession(name, cwd string) {
	tmuxKill(name)
	tmuxSilent("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", cwd)
}

func tmuxCapture(name string, lines int) string {
	out, _ := tmux("capture-pane", "-p", "-J", "-t", name+":0.0", "-S", fmt.Sprintf("-%d", lines))
	return out
}

func tmuxPaste(name, text string) {
	tmuxSilent("set-buffer", "-b", "msg", "--", text)
	tmuxSilent("paste-buffer", "-p", "-b", "msg", "-t", name+":0.0")
	time.Sleep(1500 * time.Millisecond)
	tmuxSilent("send-keys", "-t", name+":0.0", "Enter")
}

func tmuxSendLine(name, line string) {
	tmuxSilent("send-keys", "-t", name+":0.0", "--", line, "Enter")
}

func tmuxClearHistory(name string) {
	tmuxSilent("clear-history", "-t", name+":0.0")
}

func tmuxListPrefix(prefix string) []string {
	out, _ := tmux("ls", "-F", "#{session_name}")
	var names []string
	for _, s := range strings.Split(out, "\n") {
		s = strings.TrimSpace(s)
		if strings.HasPrefix(s, prefix) {
			names = append(names, s)
		}
	}
	return names
}

// sessionFor turns "nodecompat:test-crypto-hash" into a stable tmux session name.
// Prefix "db-" + sanitized ID, capped at 38 chars with sha1 suffix on overflow.
func sessionFor(taskID string) string {
	clean := reNonAlnum.ReplaceAllString(taskID, "-")
	if len(clean) <= 35 {
		return "db-" + clean
	}
	h := sha1.Sum([]byte(taskID))
	return fmt.Sprintf("db-%s-%x", clean[:28], h[:3])
}

// hashLines hashes the last n lines of pane output for idle detection.
func hashLines(pane string, n int) string {
	lines := strings.Split(pane, "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	h := sha1.Sum([]byte(strings.Join(lines, "\n")))
	return fmt.Sprintf("%x", h)
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
