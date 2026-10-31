// Package store persists the Python-compatible SQLite job lifecycle.
package store

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"time"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	_ "modernc.org/sqlite"
)

var ErrConflict = errors.New("job conflicts with persisted state")
var ErrNotFound = errors.New("job not found")

type Job struct {
	JobID             string   `json:"job_id"`
	IdempotencyKey    string   `json:"idempotency_key"`
	RequestJSON       string   `json:"request_json"`
	RequestSHA256     string   `json:"request_sha256"`
	Status            string   `json:"status"`
	CreatedAt         string   `json:"created_at"`
	UpdatedAt         string   `json:"updated_at"`
	WorkerPID         *int     `json:"worker_pid"`
	LeaseExpiresAt    *float64 `json:"lease_expires_at"`
	CancelRequestedAt *string  `json:"cancel_requested_at"`
	ResultRef         *string  `json:"result_ref"`
	ResultSHA256      *string  `json:"result_sha256"`
	ErrorCode         *string  `json:"error_code"`
	ErrorMessage      *string  `json:"error_message"`
	StdoutPath        *string  `json:"stdout_path"`
	StderrPath        *string  `json:"stderr_path"`
}
type Store struct{ db *sql.DB }

const schema = `CREATE TABLE IF NOT EXISTS backtest_jobs (job_id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,request_json TEXT NOT NULL,request_sha256 TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('SUBMITTED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,worker_pid INTEGER,lease_expires_at REAL,cancel_requested_at TEXT,result_ref TEXT,result_sha256 TEXT,error_code TEXT,error_message TEXT,stdout_path TEXT,stderr_path TEXT);`
const columns = `job_id,idempotency_key,request_json,request_sha256,status,created_at,updated_at,worker_pid,lease_expires_at,cancel_requested_at,result_ref,result_sha256,error_code,error_message,stdout_path,stderr_path`

func Open(path string, readOnly bool) (*Store, error) {
	p, e := filepath.Abs(path)
	if e != nil {
		return nil, e
	}
	mode := "rw"
	if readOnly {
		mode = "ro"
		info, e := os.Stat(p)
		if e != nil {
			return nil, e
		}
		if !info.Mode().IsRegular() {
			return nil, fmt.Errorf("database is not a regular file")
		}
	} else {
		if e = os.MkdirAll(filepath.Dir(p), 0700); e != nil {
			return nil, e
		}
		f, e := os.OpenFile(p, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
		if e == nil {
			e = f.Close()
			if e != nil {
				return nil, e
			}
		} else if !errors.Is(e, os.ErrExist) {
			return nil, e
		}
	}
	u := url.URL{Scheme: "file", Path: p}
	q := u.Query()
	q.Set("mode", mode)
	q.Add("_pragma", "busy_timeout(5000)")
	u.RawQuery = q.Encode()
	db, e := sql.Open("sqlite", u.String())
	if e != nil {
		return nil, e
	}
	db.SetMaxOpenConns(1)
	s := &Store{db: db}
	if e = db.Ping(); e == nil && !readOnly {
		_, e = db.Exec(schema)
	}
	if e != nil {
		db.Close()
		return nil, e
	}
	return s, nil
}
func (s *Store) Close() error { return s.db.Close() }
func now() string             { return time.Now().UTC().Format(time.RFC3339Nano) }
func clock() float64          { return float64(time.Now().UnixNano()) / 1e9 }

type scanner interface{ Scan(...any) error }

func scan(r scanner) (Job, error) {
	var j Job
	e := r.Scan(&j.JobID, &j.IdempotencyKey, &j.RequestJSON, &j.RequestSHA256, &j.Status, &j.CreatedAt, &j.UpdatedAt, &j.WorkerPID, &j.LeaseExpiresAt, &j.CancelRequestedAt, &j.ResultRef, &j.ResultSHA256, &j.ErrorCode, &j.ErrorMessage, &j.StdoutPath, &j.StderrPath)
	if errors.Is(e, sql.ErrNoRows) {
		e = ErrNotFound
	}
	return j, e
}
func (s *Store) Get(id string) (Job, error) {
	return scan(s.db.QueryRow("SELECT "+columns+" FROM backtest_jobs WHERE job_id=?", id))
}
func (s *Store) Lookup(key string) (Job, error) {
	return scan(s.db.QueryRow("SELECT "+columns+" FROM backtest_jobs WHERE idempotency_key=?", key))
}
func (s *Store) Submit(r *protocol.Request) (Job, bool, error) {
	if r == nil {
		return Job{}, false, fmt.Errorf("nil request")
	}
	h := sha256.Sum256([]byte(r.CanonicalJSON))
	var obj map[string]json.RawMessage
	if hex.EncodeToString(h[:]) != r.SHA256 || json.Unmarshal([]byte(r.CanonicalJSON), &obj) != nil || obj == nil {
		return Job{}, false, fmt.Errorf("request fingerprint is invalid")
	}
	var key string
	if json.Unmarshal(obj["idempotency_key"], &key) != nil || key == "" || key != r.IdempotencyKey {
		return Job{}, false, fmt.Errorf("request key does not match canonical JSON")
	}
	var b [16]byte
	if _, e := rand.Read(b[:]); e != nil {
		return Job{}, false, e
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	id := fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
	t := now()
	res, e := s.db.Exec(`INSERT INTO backtest_jobs(job_id,idempotency_key,request_json,request_sha256,status,created_at,updated_at) VALUES(?,?,?,?,'SUBMITTED',?,?) ON CONFLICT(idempotency_key) DO NOTHING`, id, key, string(r.CanonicalJSON), r.SHA256, t, t)
	if e != nil {
		return Job{}, false, e
	}
	n, e := res.RowsAffected()
	if e != nil {
		return Job{}, false, e
	}
	j, e := s.Lookup(key)
	if e == nil && j.RequestSHA256 != r.SHA256 {
		e = ErrConflict
	}
	return j, n == 1, e
}
func changed(r sql.Result, e error) (bool, error) {
	if e != nil {
		return false, e
	}
	n, e := r.RowsAffected()
	return n == 1, e
}
func (s *Store) Claim(id string, pid int, leaseSeconds float64) (bool, error) {
	if pid <= 0 || leaseSeconds <= 0 || math.IsNaN(leaseSeconds) || math.IsInf(leaseSeconds, 0) {
		return false, fmt.Errorf("invalid worker PID or lease")
	}
	return changed(s.db.Exec(`UPDATE backtest_jobs SET status='RUNNING',updated_at=?,worker_pid=?,lease_expires_at=? WHERE job_id=? AND status='SUBMITTED' AND cancel_requested_at IS NULL`, now(), pid, clock()+leaseSeconds, id))
}

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}
func (s *Store) Finish(id, status, resultRef, digest, code, message string) (bool, error) {
	return s.FinishContext(context.Background(), id, status, resultRef, digest, code, message)
}

func (s *Store) FinishContext(ctx context.Context, id, status, resultRef, digest, code, message string) (bool, error) {
	condition := "status IN ('SUBMITTED','RUNNING')"
	switch status {
	case "SUCCEEDED":
		if resultRef != "backtest-job://"+id || !digestPattern.MatchString(digest) {
			return false, fmt.Errorf("invalid result identity")
		}
		condition = "status='RUNNING' AND cancel_requested_at IS NULL"
	case "FAILED", "CANCELLED":
	default:
		return false, fmt.Errorf("illegal terminal status %q", status)
	}
	runes := []rune(message)
	if len(runes) > 2000 {
		message = string(runes[:2000])
	}
	return changed(s.db.ExecContext(ctx, `UPDATE backtest_jobs SET status=?,updated_at=?,worker_pid=NULL,lease_expires_at=NULL,result_ref=?,result_sha256=?,error_code=?,error_message=? WHERE job_id=? AND `+condition, status, now(), nullable(resultRef), nullable(digest), nullable(code), nullable(message), id))
}
func (s *Store) Cancel(id string) (Job, error) {
	t := now()
	_, e := s.db.Exec(`UPDATE backtest_jobs SET status=CASE WHEN status='SUBMITTED' THEN 'CANCELLED' ELSE status END,updated_at=?,cancel_requested_at=COALESCE(cancel_requested_at,?),worker_pid=CASE WHEN status='SUBMITTED' THEN NULL ELSE worker_pid END,lease_expires_at=CASE WHEN status='SUBMITTED' THEN NULL ELSE lease_expires_at END WHERE job_id=? AND status IN ('SUBMITTED','RUNNING')`, t, t, id)
	if e != nil {
		return Job{}, e
	}
	return s.Get(id)
}
func (s *Store) SetLogs(id, stdout, stderr string) (bool, error) {
	return changed(s.db.Exec(`UPDATE backtest_jobs SET stdout_path=?,stderr_path=?,updated_at=?,lease_expires_at=? WHERE job_id=? AND status='SUBMITTED'`, stdout, stderr, now(), clock()+60, id))
}
func (s *Store) list(query string, args ...any) ([]Job, error) {
	rows, e := s.db.Query("SELECT "+columns+" FROM backtest_jobs "+query, args...)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	jobs := []Job{}
	for rows.Next() {
		j, e := scan(rows)
		if e != nil {
			return nil, e
		}
		jobs = append(jobs, j)
	}
	return jobs, rows.Err()
}
func (s *Store) Queued() ([]Job, error) {
	return s.list("WHERE status='SUBMITTED' ORDER BY created_at,job_id")
}
func (s *Store) Expired(t float64) ([]Job, error) {
	return s.list("WHERE status='RUNNING' AND lease_expires_at<? ORDER BY lease_expires_at,job_id", t)
}
func (s *Store) ExpireUnclaimed(t float64) (int, error) {
	r, e := s.db.Exec(`UPDATE backtest_jobs SET status='FAILED',updated_at=?,worker_pid=NULL,lease_expires_at=NULL,error_code='WORKER_START_EXPIRED',error_message='Worker exited before claiming the submitted job.' WHERE status='SUBMITTED' AND lease_expires_at<?`, now(), t)
	if e != nil {
		return 0, e
	}
	n, e := r.RowsAffected()
	return int(n), e
}
