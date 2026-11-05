package app

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestQueuedInventoryChecksCancellationAndUnsafeFiles(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "stream"), 0700); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := queuedInventory(ctx, root); !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled inventory proceeded", err)
	}
	path := filepath.Join(root, "stream", "report-test")
	if err := os.Symlink(filepath.Join(root, "outside"), path); err != nil {
		t.Fatal(err)
	}
	if _, err := queuedInventory(context.Background(), root); err == nil {
		t.Fatal("accepted result symlink")
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	if err = file.Truncate(64<<20 + 1); err != nil {
		t.Fatal(err)
	}
	file.Close()
	if _, err := queuedInventory(context.Background(), root); err == nil {
		t.Fatal("accepted oversized staged file")
	}
}
