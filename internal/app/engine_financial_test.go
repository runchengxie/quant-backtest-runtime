package app

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
)

func TestEngineStaleTokenCannotPublishOrTerminalizeLiveOwner(t *testing.T) {
	root := t.TempDir()
	st, err := store.Open(filepath.Join(root, "jobs.sqlite"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	job, _, err := st.Submit(&protocol.Request{IdempotencyKey: "engine-fence", CanonicalJSON: `{"idempotency_key":"engine-fence"}`, SHA256: hash([]byte(`{"idempotency_key":"engine-fence"}`))})
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := st.Claim(job.JobID, 123, 60); err != nil || !ok {
		t.Fatal(ok, err)
	}
	if err := st.BeginAttempt(job.JobID, "live"); err != nil {
		t.Fatal(err)
	}
	stage, err := os.MkdirTemp(root, "stage-")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stage, "manifest.json"), []byte("{}"), 0600); err != nil {
		t.Fatal(err)
	}
	err = publishResult(context.Background(), Options{ResultRoot: root, attemptToken: "stale"}, st, job.JobID, stage, strings.Repeat("b", 64), syncTree, syncDirectory)
	if err == nil {
		t.Fatal("stale publication accepted")
	}
	current, err := st.Get(job.JobID)
	if err != nil || current.Status != "RUNNING" {
		t.Fatal("stale worker terminalized live owner", current.Status, err)
	}
	if _, err := os.Stat(filepath.Join(root, job.JobID)); !os.IsNotExist(err) {
		t.Fatal("stale publication remains", err)
	}
}
