package app

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/runchengxie/quant-backtest-runtime/internal/supervisor"
)

func TestMain(m *testing.M) {
	if supervisor.HandleExec(os.Args[1:]) {
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "__worker" {
		os.Exit(Run(os.Args[1:], os.Stdout, os.Stderr))
	}
	os.Exit(m.Run())
}

func TestDiscoveryDoesNotCreateStorage(t *testing.T) {
	root := t.TempDir()
	var out, err bytes.Buffer
	if Run([]string{"--database", filepath.Join(root, "absent", "jobs.sqlite"), "capabilities"}, &out, &err) != 0 {
		t.Fatal(err.String())
	}
	if !bytes.Contains(out.Bytes(), []byte("quant.agent_capabilities.v1")) {
		t.Fatal(out.String())
	}
	if _, e := os.Stat(filepath.Join(root, "absent")); !os.IsNotExist(e) {
		t.Fatal("discovery created storage")
	}
}

func TestInvalidSubmitDoesNotCreateDatabase(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "bad.json")
	_ = os.WriteFile(path, []byte(`{"schema_version":6,"idempotency_key":"bad"}`), 0600)
	var out, err bytes.Buffer
	code := Run([]string{"--error-format", "json", "--database", filepath.Join(root, "db", "jobs.sqlite"), "--artifact-root", root, "--result-root", filepath.Join(root, "results"), "submit", path}, &out, &err)
	if code == 0 || out.Len() != 0 || !bytes.Contains(err.Bytes(), []byte("INVALID_REQUEST")) {
		t.Fatalf("code=%d out=%s err=%s", code, out.String(), err.String())
	}
	if _, e := os.Stat(filepath.Join(root, "db")); !os.IsNotExist(e) {
		t.Fatal("invalid input created storage")
	}
}
