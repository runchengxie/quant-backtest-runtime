package store

import (
	"context"
	"path/filepath"
	"testing"
)

func TestCheckpointAdmissionRequiresLiveAttemptAndCommittedProgress(t *testing.T) {
	s, e := Open(filepath.Join(t.TempDir(), "jobs.sqlite"), false)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	// Direct fixture insertion isolates authority from financial parsing.
	_, e = s.db.Exec(`INSERT INTO backtest_jobs(job_id,idempotency_key,request_json,request_sha256,status,created_at,updated_at) VALUES('job','job','{}','hash','RUNNING','now','now')`)
	if e != nil {
		t.Fatal(e)
	}
	if e = s.BeginAttempt("job", "owner"); e != nil {
		t.Fatal(e)
	}
	c := Checkpoint{JobID: "job", Token: "owner", Binding: "binding", Cursor: 1, Digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
	if ok, e := s.AdmitCheckpoint(c); e != nil || !ok {
		t.Fatalf("first admission: %v %v", ok, e)
	}
	if ok, _ := s.AdmitCheckpoint(c); ok {
		t.Fatal("repeated cursor admitted")
	}
	c.Cursor = 2
	c.Token = "stale"
	if ok, _ := s.AdmitCheckpoint(c); ok {
		t.Fatal("stale owner admitted")
	}
	c.Token = "owner"
	if _, e = s.Finish("job", "FAILED", "", "", "died", ""); e != nil {
		t.Fatal(e)
	}
	if ok, _ := s.AdmitCheckpoint(c); ok {
		t.Fatal("terminal owner admitted")
	}
	got, e := s.ResumeCheckpoint("job", "binding")
	if e != nil || got.Cursor != 1 {
		t.Fatalf("failed predecessor: %+v %v", got, e)
	}
	if _, e = s.ResumeCheckpoint("job", "changed"); e == nil {
		t.Fatal("changed binding accepted")
	}
}

func TestCancelledCheckpointCannotResume(t *testing.T) {
	s, e := Open(filepath.Join(t.TempDir(), "jobs.sqlite"), false)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	_, e = s.db.Exec(`INSERT INTO backtest_jobs(job_id,idempotency_key,request_json,request_sha256,status,created_at,updated_at) VALUES('job','job','{}','hash','RUNNING','now','now')`)
	if e != nil {
		t.Fatal(e)
	}
	if e = s.BeginAttempt("job", "owner"); e != nil {
		t.Fatal(e)
	}
	if _, e = s.Cancel("job"); e != nil {
		t.Fatal(e)
	}
	if ok, _ := s.AdmitCheckpoint(Checkpoint{JobID: "job", Token: "owner", Binding: "binding", Cursor: 1, Digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}); ok {
		t.Fatal("cancelled owner admitted")
	}
	if _, e = s.ResumeCheckpoint("job", "binding"); e == nil {
		t.Fatal("cancelled predecessor accepted")
	}
}

func TestTerminalAttemptCannotPublishAfterSuccessor(t *testing.T) {
	s, e := Open(filepath.Join(t.TempDir(), "jobs.sqlite"), false)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	for _, id := range []string{"old", "new"} {
		_, e = s.db.Exec(`INSERT INTO backtest_jobs(job_id,idempotency_key,request_json,request_sha256,status,created_at,updated_at) VALUES(?,?,'{}','hash','RUNNING','now','now')`, id, id)
		if e != nil {
			t.Fatal(e)
		}
		if e = s.BeginAttempt(id, id+"-token"); e != nil {
			t.Fatal(e)
		}
	}
	if _, e = s.Finish("old", "FAILED", "", "", "died", ""); e != nil {
		t.Fatal(e)
	}
	digest := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	if ok, e := s.FinishAttempt(context.Background(), "old", "old-token", "backtest-job://old", digest); e != nil || ok {
		t.Fatalf("stale published: %v %v", ok, e)
	}
	if ok, e := s.FinishAttempt(context.Background(), "new", "old-token", "backtest-job://new", digest); e != nil || ok {
		t.Fatalf("cross-attempt published: %v %v", ok, e)
	}
	if ok, e := s.FinishAttempt(context.Background(), "new", "new-token", "backtest-job://new", digest); e != nil || !ok {
		t.Fatalf("owner denied: %v %v", ok, e)
	}
	if _, e = s.ResumeCheckpoint("new", "binding"); e == nil {
		t.Fatal("successful predecessor accepted")
	}
}
