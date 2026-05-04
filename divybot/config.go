package main

import (
	"log"
	"os"
	"time"

	"github.com/hashicorp/hcl/v2"
	"github.com/hashicorp/hcl/v2/gohcl"
	"github.com/hashicorp/hcl/v2/hclsyntax"
)

// Configurable is optionally implemented by Sources that read from config.hcl.
type Configurable interface {
	Configure(body hcl.Body) error
}

// ── HCL structs ───────────────────────────────────────────────────────────────

type FleetConfig struct {
	Claude int `hcl:"claude,optional"`
	Codex  int `hcl:"codex,optional"`
	Gemini int `hcl:"gemini,optional"`
}

type TimingConfig struct {
	WorkerPollS       int `hcl:"worker_poll_s,optional"`
	PRPollS           int `hcl:"pr_poll_s,optional"`
	FeedbackCooldownS int `hcl:"feedback_cooldown_s,optional"`
	IdleTicksCap      int `hcl:"idle_ticks_cap,optional"`
	OpenPRCap         int `hcl:"open_pr_cap,optional"`
}

type SourceBlock struct {
	Name   string   `hcl:"name,label"`
	Remain hcl.Body `hcl:",remain"`
}

type rawConfig struct {
	Fleet   *FleetConfig   `hcl:"fleet,block"`
	Timing  *TimingConfig  `hcl:"timing,block"`
	Sources []SourceBlock  `hcl:"source,block"`
}

// ── Config ────────────────────────────────────────────────────────────────────

type Config struct {
	Fleet  FleetConfig
	Timing TimingConfig
}

var (
	defaultFleet  = FleetConfig{Claude: 8}
	defaultTiming = TimingConfig{
		WorkerPollS:       20,
		PRPollS:           180,
		FeedbackCooldownS: 600,
		IdleTicksCap:      4,
		OpenPRCap:         25,
	}
)

func (c *Config) WorkerPoll() time.Duration {
	return time.Duration(c.Timing.WorkerPollS) * time.Second
}
func (c *Config) PRPoll() time.Duration {
	return time.Duration(c.Timing.PRPollS) * time.Second
}
func (c *Config) FeedbackCooldown() time.Duration {
	return time.Duration(c.Timing.FeedbackCooldownS) * time.Second
}
func (c *Config) TotalCapacity() int {
	return c.Fleet.Claude + c.Fleet.Codex + c.Fleet.Gemini
}

// pickCLI returns the first CLI with free capacity given current running counts.
func (c *Config) pickCLI(running map[string]int) (string, bool) {
	type slot struct{ cli string; cap int }
	slots := []slot{
		{"claude", c.Fleet.Claude},
		{"codex", c.Fleet.Codex},
		{"gemini", c.Fleet.Gemini},
	}
	for _, s := range slots {
		if s.cap > 0 && running[s.cli] < s.cap {
			return s.cli, true
		}
	}
	return "", false
}

// ── Load ──────────────────────────────────────────────────────────────────────

func loadConfig(path string, srcs map[string]Source) *Config {
	cfg := &Config{Fleet: defaultFleet, Timing: defaultTiming}

	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return cfg
	}
	if err != nil {
		log.Printf("config: read %s: %v — using defaults", path, err)
		return cfg
	}

	f, diags := hclsyntax.ParseConfig(data, path, hcl.Pos{Line: 1, Column: 1})
	if diags.HasErrors() {
		log.Printf("config: parse error: %s — using defaults", diags.Error())
		return cfg
	}

	var raw rawConfig
	if diags := gohcl.DecodeBody(f.Body, nil, &raw); diags.HasErrors() {
		log.Printf("config: decode error: %s — using defaults", diags.Error())
		return cfg
	}

	if raw.Fleet != nil {
		cfg.Fleet = *raw.Fleet
	}
	if raw.Timing != nil {
		t := defaultTiming
		if raw.Timing.WorkerPollS > 0 {
			t.WorkerPollS = raw.Timing.WorkerPollS
		}
		if raw.Timing.PRPollS > 0 {
			t.PRPollS = raw.Timing.PRPollS
		}
		if raw.Timing.FeedbackCooldownS > 0 {
			t.FeedbackCooldownS = raw.Timing.FeedbackCooldownS
		}
		if raw.Timing.IdleTicksCap > 0 {
			t.IdleTicksCap = raw.Timing.IdleTicksCap
		}
		if raw.Timing.OpenPRCap > 0 {
			t.OpenPRCap = raw.Timing.OpenPRCap
		}
		cfg.Timing = t
	}

	for _, sb := range raw.Sources {
		src, ok := srcs[sb.Name]
		if !ok {
			log.Printf("config: unknown source %q — skipping", sb.Name)
			continue
		}
		if c, ok := src.(Configurable); ok {
			if err := c.Configure(sb.Remain); err != nil {
				log.Printf("config: source %s configure: %v", sb.Name, err)
			}
		}
	}

	return cfg
}
