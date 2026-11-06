package store

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

func retryDatabase(t *testing.T) (*Store, *Store, string) {
	t.Helper()
	path := t.TempDir() + "/jobs.db"
	s, err := OpenScheduler(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	locker, err := Open(path, false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close(); locker.Close() })
	job := submit(t, s, "retry")
	return s, locker, job.JobID
}

func TestRetrySchedulerStatements(t *testing.T) {
	for _, method := range []string{"get", "list", "logs", "claim", "finish", "expire"} {
		t.Run(method, func(t *testing.T) {
			s, locker, id := retryDatabase(t)
			if method == "finish" {
				if _, err := s.Claim(id, 123, 60); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := locker.db.Exec("BEGIN EXCLUSIVE"); err != nil {
				t.Fatal(err)
			}
			done := make(chan error, 1)
			go func() {
				var err error
				switch method {
				case "get":
					_, err = s.Get(id)
				case "list":
					_, err = s.Queued()
				case "logs":
					_, err = s.SetLogs(id, "out", "err")
				case "claim":
					_, err = s.Claim(id, 123, 60)
				case "finish":
					_, err = s.Finish(id, "SUCCEEDED", "backtest-job://"+id, strings.Repeat("a", 64), "", "")
				case "expire":
					_, err = s.ExpireUnclaimed(float64(time.Now().Unix()))
				}
				done <- err
			}()
			select {
			case err := <-done:
				t.Fatalf("returned during reservation: %v", err)
			case <-time.After(350 * time.Millisecond):
			}
			if _, err := locker.db.Exec("ROLLBACK"); err != nil {
				t.Fatal(err)
			}
			select {
			case err := <-done:
				if err != nil {
					t.Fatal(err)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("did not resume")
			}
		})
	}
}

func TestRetryCancellationAndDefaultRestoration(t *testing.T) {
	s, locker, id := retryDatabase(t)
	ctx, cancel := context.WithCancel(context.Background())
	s.SetRetryContext(ctx)
	if _, err := locker.db.Exec("BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := s.SetLogs(id, "out", "err"); done <- err }()
	time.Sleep(250 * time.Millisecond)
	started := time.Now()
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("cancel did not interrupt wait")
	}
	if time.Since(started) > time.Second {
		t.Fatal("slow cancellation")
	}
	locker.db.Exec("ROLLBACK")
	job, err := locker.Get(id)
	if err != nil || job.LeaseExpiresAt != nil {
		t.Fatalf("startup mutated: %+v %v", job, err)
	}
	s.SetRetryContext(context.Background())
	if err = s.EndStartupRetry(); err != nil {
		t.Fatal(err)
	}
	s.db.SetMaxIdleConns(0) // Force a fresh connection: defaults must survive reconnection.
	var timeout int
	if err = s.db.QueryRow("PRAGMA busy_timeout").Scan(&timeout); err != nil || timeout != 5000 || s.retryContext != nil {
		t.Fatal(timeout, err)
	}
}

func TestRetryOnlyTypedSQLiteContention(t *testing.T) {
	s, locker, _ := retryDatabase(t)
	locker.db.Exec("BEGIN IMMEDIATE")
	_, busy := s.db.Exec("UPDATE backtest_jobs SET updated_at='x'")
	locker.db.Exec("ROLLBACK")
	if !IsBusy(fmt.Errorf("wrapped: %w", busy)) || IsBusy(errors.New("database is locked (5) SQLITE_BUSY")) {
		t.Fatal("wrong contention classification")
	}
	attempts := 0
	parent, cancel := context.WithTimeout(context.Background(), 180*time.Millisecond)
	defer cancel()
	_, err := retry(s, parent, func(context.Context) (int, error) { attempts++; return 0, busy })
	if !errors.Is(err, context.DeadlineExceeded) || attempts > 4 || attempts < 2 {
		t.Fatal(attempts, err)
	}
	attempts = 0
	expected := errors.New("disk full")
	_, err = retry(s, context.Background(), func(context.Context) (int, error) { attempts++; return 0, expected })
	if err != expected || attempts != 1 {
		t.Fatal(attempts, err)
	}
	if _, err = s.db.Exec("DROP TABLE backtest_jobs"); err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	if _, err = s.Queued(); err == nil || IsBusy(err) || time.Since(started) > time.Second {
		t.Fatal("nonbusy SQL error was retried", err)
	}
}
