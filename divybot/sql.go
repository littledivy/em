package main

import (
	"database/sql"
	"fmt"
	"log"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

const schema = `
CREATE TABLE IF NOT EXISTS tasks (
	id               TEXT PRIMARY KEY,
	status           TEXT NOT NULL,
	cli              TEXT NOT NULL DEFAULT 'claude',
	session_id       TEXT,
	pr_url           TEXT,
	branch           TEXT,
	attempts         INTEGER NOT NULL DEFAULT 0,
	idle_ticks       INTEGER NOT NULL DEFAULT 0,
	last_hash        TEXT,
	last_pr_hash     TEXT,
	last_error       TEXT,
	last_feedback_at INTEGER,
	created_at       INTEGER NOT NULL,
	updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON tasks(status);
`

// Task mirrors a row in the tasks table.
type Task struct {
	ID             string
	Status         string
	CLI            string
	SessionID      string
	PRURL          string
	Branch         string
	Attempts       int
	IdleTicks      int
	LastHash       string
	LastPRHash     string
	LastError      string
	LastFeedbackAt int64
	CreatedAt      int64
	UpdatedAt      int64
}

// DB wraps a sqlite connection.
type DB struct{ conn *sql.DB }

func mustOpenDB(path string) *DB {
	conn, err := sql.Open("sqlite", path+"?_journal=WAL&_timeout=5000")
	if err != nil {
		log.Fatalf("open db %s: %v", path, err)
	}
	if _, err := conn.Exec(schema); err != nil {
		log.Fatalf("init schema: %v", err)
	}
	return &DB{conn}
}

func (db *DB) Close() { db.conn.Close() }

func (db *DB) scan(row *sql.Row) *Task {
	t := &Task{}
	var sid, prURL, branch, lastHash, lastPRHash, lastErr sql.NullString
	var lastFeedback sql.NullInt64
	err := row.Scan(
		&t.ID, &t.Status, &t.CLI, &sid, &prURL, &branch,
		&t.Attempts, &t.IdleTicks, &lastHash, &lastPRHash,
		&lastErr, &lastFeedback, &t.CreatedAt, &t.UpdatedAt,
	)
	if err != nil {
		return nil
	}
	t.SessionID = sid.String
	t.PRURL = prURL.String
	t.Branch = branch.String
	t.LastHash = lastHash.String
	t.LastPRHash = lastPRHash.String
	t.LastError = lastErr.String
	t.LastFeedbackAt = lastFeedback.Int64
	return t
}

const taskCols = `id,status,cli,session_id,pr_url,branch,attempts,idle_ticks,last_hash,last_pr_hash,last_error,last_feedback_at,created_at,updated_at`

func (db *DB) Get(id string) *Task {
	row := db.conn.QueryRow(`SELECT `+taskCols+` FROM tasks WHERE id=?`, id)
	return db.scan(row)
}

func (db *DB) Insert(id, status, cli, sid, branch string) {
	now := time.Now().Unix()
	_, err := db.conn.Exec(
		`INSERT OR REPLACE INTO tasks(id,status,cli,session_id,branch,attempts,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)`,
		id, status, cli, sid, branch, now, now,
	)
	if err != nil {
		log.Printf("db insert %s: %v", id, err)
	}
}

func (db *DB) Update(id string, fields map[string]any) {
	fields["updated_at"] = time.Now().Unix()
	cols := make([]string, 0, len(fields))
	vals := make([]any, 0, len(fields)+1)
	for k, v := range fields {
		cols = append(cols, k+"=?")
		vals = append(vals, v)
	}
	vals = append(vals, id)
	q := fmt.Sprintf(`UPDATE tasks SET %s WHERE id=?`, strings.Join(cols, ","))
	if _, err := db.conn.Exec(q, vals...); err != nil {
		log.Printf("db update %s: %v", id, err)
	}
}

func (db *DB) scanRows(rows *sql.Rows) []Task {
	defer rows.Close()
	var out []Task
	for rows.Next() {
		t := Task{}
		var sid, prURL, branch, lastHash, lastPRHash, lastErr sql.NullString
		var lastFeedback sql.NullInt64
		if err := rows.Scan(
			&t.ID, &t.Status, &t.CLI, &sid, &prURL, &branch,
			&t.Attempts, &t.IdleTicks, &lastHash, &lastPRHash,
			&lastErr, &lastFeedback, &t.CreatedAt, &t.UpdatedAt,
		); err != nil {
			continue
		}
		t.SessionID = sid.String
		t.PRURL = prURL.String
		t.Branch = branch.String
		t.LastHash = lastHash.String
		t.LastPRHash = lastPRHash.String
		t.LastError = lastErr.String
		t.LastFeedbackAt = lastFeedback.Int64
		out = append(out, t)
	}
	return out
}

func (db *DB) WithStatus(status string) []Task {
	rows, err := db.conn.Query(`SELECT `+taskCols+` FROM tasks WHERE status=?`, status)
	if err != nil {
		return nil
	}
	return db.scanRows(rows)
}

func (db *DB) WithStatuses(statuses ...string) []Task {
	ph := strings.Repeat("?,", len(statuses))
	ph = ph[:len(ph)-1]
	args := make([]any, len(statuses))
	for i, s := range statuses {
		args[i] = s
	}
	rows, err := db.conn.Query(`SELECT `+taskCols+` FROM tasks WHERE status IN (`+ph+`)`, args...)
	if err != nil {
		return nil
	}
	return db.scanRows(rows)
}

func (db *DB) RunningCounts() map[string]int {
	rows, err := db.conn.Query(`SELECT cli, COUNT(*) FROM tasks WHERE status='running' GROUP BY cli`)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var cli string
		var n int
		rows.Scan(&cli, &n)
		out[cli] = n
	}
	return out
}

func (db *DB) TotalOpen() int {
	var n int
	db.conn.QueryRow(`SELECT COUNT(*) FROM tasks WHERE status IN ('review','running')`).Scan(&n)
	return n
}

func (db *DB) PrintStatus() {
	rows, err := db.conn.Query(
		`SELECT id,status,cli,pr_url,attempts,last_error FROM tasks ORDER BY updated_at DESC LIMIT 40`,
	)
	if err != nil {
		log.Printf("status query: %v", err)
		return
	}
	defer rows.Close()
	fmt.Printf("%-42s %-10s %-7s %-4s %s\n", "TASK", "STATUS", "CLI", "ATMP", "PR/ERROR")
	fmt.Println(strings.Repeat("-", 100))
	for rows.Next() {
		var id, status, cli string
		var prURL, lastErr sql.NullString
		var attempts int
		rows.Scan(&id, &status, &cli, &prURL, &attempts, &lastErr)
		extra := prURL.String
		if extra == "" {
			extra = lastErr.String
		}
		if len(extra) > 50 {
			extra = extra[:50]
		}
		fmt.Printf("%-42s %-10s %-7s %-4d %s\n", id, status, cli, attempts, extra)
	}
}
