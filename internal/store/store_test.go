package store

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
)

func request(key string) *protocol.Request {
	b := []byte(`{"idempotency_key":"` + key + `"}`)
	h := sha256.Sum256(b)
	return &protocol.Request{IdempotencyKey: key, CanonicalJSON: string(b), SHA256: hex.EncodeToString(h[:])}
}
func database(t *testing.T) *Store {
	t.Helper()
	s, e := Open(filepath.Join(t.TempDir(), "jobs.db"), false)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.Close() })
	return s
}
func submit(t *testing.T, s *Store, key string) Job {
	t.Helper()
	j, c, e := s.Submit(request(key))
	if e != nil || !c {
		t.Fatalf("submit %v %v", c, e)
	}
	return j
}
func TestIdempotencyAndTampering(t *testing.T) {
	s := database(t)
	j := submit(t, s, "one")
	got, c, e := s.Submit(request("one"))
	if e != nil || c || got.JobID != j.JobID {
		t.Fatal(got, c, e)
	}
	r := request("one")
	r.CanonicalJSON = `{"idempotency_key":"one","other":1}`
	h := sha256.Sum256([]byte(r.CanonicalJSON))
	r.SHA256 = hex.EncodeToString(h[:])
	if _, _, e = s.Submit(r); !errors.Is(e, ErrConflict) {
		t.Fatal(e)
	}
	r = request("tamper")
	r.SHA256 = "bad"
	if _, _, e = s.Submit(r); e == nil {
		t.Fatal("accepted tampered hash")
	}
	if _, e = s.Get("missing"); !errors.Is(e, ErrNotFound) {
		t.Fatal(e)
	}
}
func TestClaimsAndCancellation(t *testing.T) {
	s := database(t)
	j := submit(t, s, "claim")
	var wins atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ok, e := s.Claim(j.JobID, 123, 10)
			if e != nil {
				t.Error(e)
			}
			if ok {
				wins.Add(1)
			}
		}()
	}
	wg.Wait()
	if wins.Load() != 1 {
		t.Fatal(wins.Load())
	}
	if _, e := s.Cancel(j.JobID); e != nil {
		t.Fatal(e)
	}
	if ok, e := s.Finish(j.JobID, "SUCCEEDED", "backtest-job://"+j.JobID, strings.Repeat("a", 64), "", ""); e == nil && ok {
		t.Fatal("success after cancel")
	}
	if ok, e := s.Finish(j.JobID, "CANCELLED", "", "", "", ""); !ok || e != nil {
		t.Fatal(ok, e)
	}
	got, _ := s.Get(j.JobID)
	if got.WorkerPID != nil || got.LeaseExpiresAt != nil || got.CancelRequestedAt == nil {
		t.Fatal(got)
	}
	if ok, _ := s.Finish(j.JobID, "FAILED", "", "", "late", "late"); ok {
		t.Fatal("overwrote terminal")
	}
}
func TestQueueExpiryAndReadonly(t *testing.T) {
	s := database(t)
	j := submit(t, s, "queue")
	q, e := s.Queued()
	if e != nil || len(q) != 1 {
		t.Fatal(q, e)
	}
	if ok, e := s.SetLogs(j.JobID, "out", "err"); !ok || e != nil {
		t.Fatal(ok, e)
	}
	if n, e := s.ExpireUnclaimed(float64(time.Now().Unix() + 100)); n != 1 || e != nil {
		t.Fatal(n, e)
	}
	got, _ := s.Get(j.JobID)
	if got.Status != "FAILED" || got.ErrorCode == nil || *got.ErrorCode != "WORKER_START_EXPIRED" {
		t.Fatal(got)
	}
	if _, e := Open(filepath.Join(t.TempDir(), "missing"), true); e == nil {
		t.Fatal("created readonly database")
	}
}

func TestCancelCompletionRace(t *testing.T) {
	s := database(t)
	for i := 0; i < 30; i++ {
		j := submit(t, s, fmt.Sprint("race", i))
		s.Claim(j.JobID, 123, 10)
		var wg sync.WaitGroup
		wg.Add(2)
		go func() {
			defer wg.Done()
			_, e := s.Cancel(j.JobID)
			if e != nil {
				t.Error(e)
			}
		}()
		go func() {
			defer wg.Done()
			_, e := s.Finish(j.JobID, "SUCCEEDED", "backtest-job://"+j.JobID, strings.Repeat("a", 64), "", "")
			if e != nil {
				t.Error(e)
			}
		}()
		wg.Wait()
		got, _ := s.Get(j.JobID)
		if got.Status == "SUCCEEDED" && got.CancelRequestedAt != nil {
			t.Fatal("success persisted despite cancellation")
		}
	}
}
func TestCompetingSubmissions(t *testing.T) {
	s := database(t)
	var wg sync.WaitGroup
	var wins atomic.Int32
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, created, e := s.Submit(request("same"))
			if e != nil {
				t.Error(e)
			}
			if created {
				wins.Add(1)
			}
		}()
	}
	wg.Wait()
	if wins.Load() != 1 {
		t.Fatal(wins.Load())
	}
}
func TestIllegalTransitionsAndExpiry(t *testing.T) {
	s := database(t)
	j := submit(t, s, "illegal")
	if _, e := s.Claim(j.JobID, -1, 1); e == nil {
		t.Fatal("negative PID")
	}
	if _, e := s.Finish(j.JobID, "RUNNING", "", "", "", ""); e == nil {
		t.Fatal("nonterminal Finish")
	}
	if ok, e := s.Finish(j.JobID, "SUCCEEDED", "backtest-job://"+j.JobID, strings.Repeat("a", 64), "", ""); ok || e != nil {
		t.Fatal(ok, e)
	}
	s.Claim(j.JobID, 123, 1)
	rows, e := s.Expired(float64(time.Now().Unix() + 100))
	if e != nil || len(rows) != 1 {
		t.Fatal(rows, e)
	}
	if ok, e := s.Finish(j.JobID, "SUCCEEDED", "backtest-job://"+j.JobID, strings.Repeat("a", 64), "", ""); !ok || e != nil {
		t.Fatal(ok, e)
	}
	got, _ := s.Get(j.JobID)
	if got.ResultSHA256 == nil || got.WorkerPID != nil || got.LeaseExpiresAt != nil {
		t.Fatal(got)
	}
}
func TestPythonDatabaseAndReadonly(t *testing.T) {
	p := filepath.Join(t.TempDir(), "python.db")
	script := `import sqlite3,sys
c=sqlite3.connect(sys.argv[1])
from pathlib import Path
import runpy
store = runpy.run_path(str(Path("../../src/backtest_runtime/store.py")))
owner = store["JobStore"](sys.argv[1])
owner.close()
c.execute("INSERT INTO backtest_jobs(job_id,idempotency_key,request_json,request_sha256,status,created_at,updated_at) VALUES('old','old','{}','hash','SUBMITTED','2026-01-01','2026-01-01')")
c.commit()
`
	if out, e := exec.Command("python3", "-c", script, p).CombinedOutput(); e != nil {
		t.Fatalf("%s %v", out, e)
	}
	s, e := Open(p, true)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	j, e := s.Get("old")
	if e != nil || j.RequestJSON != "{}" || j.WorkerPID != nil {
		t.Fatal(j, e)
	}
	if _, _, e := s.Submit(request("write")); e == nil {
		t.Fatal("readonly accepted write")
	}
	info, e := os.Stat(p)
	if e != nil || info.Size() == 0 {
		t.Fatal(info, e)
	}
}

func TestDatabasePermissions(t *testing.T) {
	parent := t.TempDir()
	if e := os.Chmod(parent, 0755); e != nil {
		t.Fatal(e)
	}
	p := filepath.Join(parent, "private", "db.sqlite")
	s, e := Open(p, false)
	if e != nil {
		t.Fatal(e)
	}
	s.Close()
	for path, want := range map[string]os.FileMode{parent: 0755, filepath.Dir(p): 0700, p: 0600} {
		info, e := os.Stat(path)
		if e != nil || info.Mode().Perm() != want {
			t.Fatalf("%s: %v %v", path, info, e)
		}
	}
}
