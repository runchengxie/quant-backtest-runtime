package store

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestFinishContextCancelled(t *testing.T) {
	s := database(t)
	j := submit(t, s, "cancelled-context")
	s.Claim(j.JobID, 123, 60)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	ok, err := s.FinishContext(ctx, j.JobID, "SUCCEEDED", "backtest-job://"+j.JobID, strings.Repeat("a", 64), "", "")
	if ok || !errors.Is(err, context.Canceled) {
		t.Fatalf("finish = %v, %v", ok, err)
	}
	got, _ := s.Get(j.JobID)
	if got.Status != "RUNNING" {
		t.Fatal(got.Status)
	}
}

func TestFinishContextLockWaitDeadline(t *testing.T) {
	s := database(t)
	j := submit(t, s, "locked-context")
	s.Claim(j.JobID, 123, 60)
	conn, err := s.db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// Hold the sole connection: execution must not start after its budget expires.
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := s.FinishContext(ctx, j.JobID, "SUCCEEDED", "backtest-job://"+j.JobID, strings.Repeat("a", 64), "", "")
		done <- err
	}()
	<-ctx.Done()
	conn.Close()
	if err := <-done; !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	got, _ := s.Get(j.JobID)
	if got.Status != "RUNNING" {
		t.Fatal(got.Status)
	}
}

func TestFinishContextSQLiteLockDeadline(t *testing.T) {
	path := t.TempDir() + "/jobs.db"
	s, err := Open(path, false)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	j := submit(t, s, "sqlite-lock")
	s.Claim(j.JobID, 123, 60)
	locker, err := Open(path, false)
	if err != nil {
		t.Fatal(err)
	}
	defer locker.Close()
	if _, err = locker.db.Exec("BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := s.FinishContext(ctx, j.JobID, "SUCCEEDED", "backtest-job://"+j.JobID, strings.Repeat("a", 64), "", "")
		done <- err
	}()
	<-ctx.Done()
	// Release the writer only after the deadline; no late success may commit.
	if _, err = locker.db.Exec("ROLLBACK"); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err == nil {
		t.Fatal("late finish succeeded")
	}
	got, err := s.Get(j.JobID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != "RUNNING" {
		t.Fatal(got.Status)
	}
}
