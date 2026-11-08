package app

import (
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
	"os"
	"path/filepath"
	"testing"
)

func TestFinancialCheckpointRejectsNamespaceSymlink(t *testing.T) {
	root := t.TempDir()
	o := Options{ResultRoot: root}
	c := store.Checkpoint{JobID: "00000000-0000-0000-0000-000000000000", Token: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", Binding: "binding", Cursor: 1}
	dir := filepath.Dir(checkpointPath(o, c))
	if e := os.MkdirAll(dir, 0700); e != nil {
		t.Fatal(e)
	}
	raw := []byte(`{"schema":"runtime-minute-checkpoint.v1","job_id":"00000000-0000-0000-0000-000000000000","token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","binding":"binding","cursor":1,"checkpoint":{"schema":"minute-session.v2","cursor":1}}`)
	c.Digest = hash(raw)
	if e := os.WriteFile(checkpointPath(o, c), raw, 0600); e != nil {
		t.Fatal(e)
	}
	if _, e := checkpointBytes(o, c); e != nil {
		t.Fatal(e)
	}
	moved := filepath.Join(root, "elsewhere")
	if e := os.Rename(dir, moved); e != nil {
		t.Fatal(e)
	}
	if e := os.Symlink(moved, dir); e != nil {
		t.Fatal(e)
	}
	if _, e := checkpointBytes(o, c); e == nil {
		t.Fatal("checkpoint followed a namespace symlink")
	}
}
