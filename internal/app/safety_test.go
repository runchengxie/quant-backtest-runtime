package app

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDiscoveryAdvertisesQueueAndMemoryBoundary(t *testing.T) {
	var out, err bytes.Buffer
	if Run([]string{"capabilities"}, &out, &err) != 0 {
		t.Fatal(err.String())
	}
	var caps struct {
		Operations []struct {
			ID string `json:"id"`
		} `json:"operations"`
		Limits map[string]any `json:"resource_limits"`
	}
	if e := json.Unmarshal(out.Bytes(), &caps); e != nil {
		t.Fatal(e)
	}
	found := false
	for _, op := range caps.Operations {
		found = found || op.ID == "serve"
	}
	if !found {
		t.Fatal("queue operation missing from discovery")
	}
	if caps.Limits["go_orchestration_memory_included"] != false {
		t.Fatal("memory scope missing")
	}
}

func TestStorageErrorCodesRemainCompatible(t *testing.T) {
	for _, mode := range []string{"missing", "corrupt"} {
		t.Run(mode, func(t *testing.T) {
			root := t.TempDir()
			database := filepath.Join(root, "jobs.sqlite")
			command := "inspect"
			expected := "DATABASE_UNAVAILABLE"
			if mode == "corrupt" {
				_ = os.WriteFile(database, []byte("not SQLite"), 0600)
				command = "status"
				expected = "STORAGE_FAILURE"
			}
			var out, err bytes.Buffer
			code := Run([]string{"--error-format=json", "--database", database, "--artifact-root", root, "--result-root", root, command, "00000000-0000-4000-8000-000000000001"}, &out, &err)
			var failure map[string]any
			if json.Unmarshal(err.Bytes(), &failure) != nil || code != 1 || failure["code"] != expected {
				t.Fatalf("code=%d stderr=%s", code, err.String())
			}
		})
	}
}

func TestDuplicateLegacyKeysFailBeforeAdapter(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "request.json")
	_ = os.WriteFile(path, []byte(`{"schema_version":4,"schema_version":4}`), 0600)
	_, e := loadRequest(context.Background(), Options{}, path)
	if e == nil || !strings.Contains(e.Error(), "duplicate") {
		t.Fatalf("duplicate not rejected before Python: %v", e)
	}
}

func nativeRequest(ref string) map[string]any {
	return map[string]any{"schema_version": 5, "idempotency_key": "preflight", "backend": "nautilus.quote_market_orders", "evidence_tier": "diagnostic", "inputs": map[string]any{"engine_request_ref": ref}, "config": map[string]any{"executable_sha256": strings.Repeat("a", 64)}, "execution": map[string]any{}, "budgets": map[string]any{"wall_seconds": 10, "memory_mb": 4096}}
}

func TestMissingArtifactPreflightCreatesNoState(t *testing.T) {
	root := t.TempDir()
	data, _ := json.Marshal(nativeRequest("artifact://sha256/" + strings.Repeat("b", 64)))
	path := filepath.Join(root, "request.json")
	_ = os.WriteFile(path, data, 0600)
	var out, err bytes.Buffer
	code := Run([]string{"--error-format=json", "--database", filepath.Join(root, "new", "jobs.sqlite"), "--artifact-root", root, "--result-root", filepath.Join(root, "results"), "--engine-executable", "/bin/false", "submit", path}, &out, &err)
	if code != 1 || out.Len() != 0 {
		t.Fatal(code, out.String(), err.String())
	}
	if _, e := os.Stat(filepath.Join(root, "new")); !os.IsNotExist(e) {
		t.Fatal("preflight failure created database directory")
	}
}
