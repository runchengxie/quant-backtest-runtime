package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
)

func TestPublicationDeadlineAfterDirectorySync(t *testing.T) {
	root := t.TempDir()
	st, err := store.Open(filepath.Join(root, "jobs.db"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	raw := `{"idempotency_key":"publication-deadline"}`
	sum := sha256.Sum256([]byte(raw))
	j, _, err := st.Submit(&protocol.Request{IdempotencyKey: "publication-deadline", CanonicalJSON: raw, SHA256: hex.EncodeToString(sum[:])})
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := st.Claim(j.JobID, 123, 60); !ok || err != nil {
		t.Fatal(ok, err)
	}
	stage, err := os.MkdirTemp(root, "."+j.JobID+".tmp-")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stage, "manifest.json"), []byte("valid"), 0600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	// Let the deadline expire inside directory fsync, after the atomic rename.
	err = publishResult(ctx, Options{ResultRoot: root}, st, j.JobID, stage, strings.Repeat("a", 64), syncTree, func(path string) error { <-ctx.Done(); return syncDirectory(path) })
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	got, err := st.Get(j.JobID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != "FAILED" || got.ErrorCode == nil || *got.ErrorCode != "JOB_TIMEOUT" {
		t.Fatalf("%+v", got)
	}
	if _, err := os.Stat(filepath.Join(root, j.JobID)); !os.IsNotExist(err) {
		t.Fatalf("published output remains: %v", err)
	}
}

func TestPublicationRetainsFilesWhenCommitStateUnknown(t *testing.T) {
	root := t.TempDir()
	st, err := store.Open(filepath.Join(root, "jobs.db"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	raw := `{"idempotency_key":"publication-deadline"}`
	sum := sha256.Sum256([]byte(raw))
	j, _, err := st.Submit(&protocol.Request{IdempotencyKey: "publication-deadline", CanonicalJSON: raw, SHA256: hex.EncodeToString(sum[:])})
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := st.Claim(j.JobID, 123, 60); !ok || err != nil {
		t.Fatal(ok, err)
	}
	stage, err := os.MkdirTemp(root, "."+j.JobID+".tmp-")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stage, "manifest.json"), []byte("valid"), 0600); err != nil {
		t.Fatal(err)
	}

	err = publishResult(context.Background(), Options{ResultRoot: root}, st, j.JobID, stage, strings.Repeat("a", 64), syncTree, func(path string) error {
		if err := syncDirectory(path); err != nil {
			return err
		}
		return st.Close()
	})
	if err == nil {
		t.Fatal("expected closed-store error")
	}
	if _, err := os.Stat(filepath.Join(root, j.JobID, "manifest.json")); err != nil {
		t.Fatalf("uncertain publication lost: %v", err)
	}
}

func TestPublicationRetainsAlreadyCommittedSuccess(t *testing.T) {
	root := t.TempDir()
	st, err := store.Open(filepath.Join(root, "jobs.db"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	raw := `{"idempotency_key":"publication-deadline"}`
	sum := sha256.Sum256([]byte(raw))
	j, _, err := st.Submit(&protocol.Request{IdempotencyKey: "publication-deadline", CanonicalJSON: raw, SHA256: hex.EncodeToString(sum[:])})
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := st.Claim(j.JobID, 123, 60); !ok || err != nil {
		t.Fatal(ok, err)
	}
	stage, err := os.MkdirTemp(root, "."+j.JobID+".tmp-")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stage, "manifest.json"), []byte("valid"), 0600); err != nil {
		t.Fatal(err)
	}

	err = publishResult(context.Background(), Options{ResultRoot: root}, st, j.JobID, stage, strings.Repeat("a", 64), syncTree, func(path string) error {
		if err := syncDirectory(path); err != nil {
			return err
		}
		_, err := st.Finish(j.JobID, "SUCCEEDED", "backtest-job://"+j.JobID, strings.Repeat("a", 64), "", "")
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, j.JobID, "manifest.json")); err != nil {
		t.Fatalf("committed publication lost: %v", err)
	}
}
