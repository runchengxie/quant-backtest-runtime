package app

import (
	"bytes"
	"github.com/runchengxie/quant-backtest-runtime/internal/jobgroup"
	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCgroupServeOptionAndFailClosed(t *testing.T) {
	root := t.TempDir()
	args := []string{"serve", "--cgroup-root", root, "--database", filepath.Join(root, "jobs.sqlite"), "--artifact-root", root, "--result-root", filepath.Join(root, "results"), "--error-format", "json", "--once"}
	o, e := parse(args)
	if e != nil || o.CgroupRoot != root {
		t.Fatalf("parse: %#v %v", o, e)
	}
	var out, errout bytes.Buffer
	if Run(args, &out, &errout) == 0 || !bytes.Contains(errout.Bytes(), []byte("RESOURCE_LIMIT_UNAVAILABLE")) {
		t.Fatalf("out=%s err=%s", out.String(), errout.String())
	}
	if _, e := os.Stat(o.Database); !os.IsNotExist(e) {
		t.Fatal("unavailable cgroup created database")
	}
}

func TestAbandonedWorkerReconciliationPreservesCommittedSuccess(t *testing.T) {
	root := t.TempDir()
	st, err := store.Open(filepath.Join(root, "db"), false)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	j, _, err := st.Submit(&protocol.Request{IdempotencyKey: "cas-success", CanonicalJSON: `{"idempotency_key":"cas-success"}`, SHA256: hash([]byte(`{"idempotency_key":"cas-success"}`))})
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := st.Claim(j.JobID, 123, 60); !ok || err != nil {
		t.Fatal(ok, err)
	}
	stale, err := st.Get(j.JobID)
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := st.Finish(j.JobID, "SUCCEEDED", "backtest-job://"+j.JobID, strings.Repeat("b", 64), "", ""); !ok || err != nil {
		t.Fatal(ok, err)
	}
	output := filepath.Join(root, j.JobID)
	if err = os.Mkdir(output, 0700); err != nil {
		t.Fatal(err)
	}
	if err = failAbandoned(Options{ResultRoot: root}, st, stale, "WORKER_EXITED"); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(output); err != nil {
		t.Fatal("removed successful output", err)
	}
}

func TestResourceReceiptPrecedesGroupRemoval(t *testing.T) {
	if os.Getenv("JOBGROUP_INTEGRATION") != "1" {
		t.Skip("requires delegated cgroup")
	}
	root := t.TempDir()
	groups, err := jobgroup.New("auto", filepath.Join(root, "db"))
	if err != nil {
		t.Fatal(err)
	}
	id := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	fd, err := groups.Create(id, 256)
	if err != nil {
		t.Fatal(err)
	}
	fd.Close()
	defer groups.Cleanup(id)
	output := filepath.Join(root, "not-a-directory")
	if err = os.WriteFile(output, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	o := Options{ResultRoot: output, groups: groups}
	if finishGroup(o, id) == nil {
		t.Fatal("expected receipt storage failure")
	}
	if _, err = groups.Inspect(id); err != nil {
		t.Fatalf("lost counters before receipt: %v", err)
	}
	o.ResultRoot = filepath.Join(root, "results")
	if err = finishGroup(o, id); err != nil {
		t.Fatal(err)
	}
	if _, err = groups.Inspect(id); !os.IsNotExist(err) {
		t.Fatalf("group still exists: %v", err)
	}
}

func TestCgroupOptionCannotPromiseImmediateJobIsolation(t *testing.T) {
	_, e := parse([]string{"submit", "request.json", "--database", "/tmp/db", "--artifact-root", "/tmp/a", "--result-root", "/tmp/r", "--cgroup-root", "auto"})
	if e == nil {
		t.Fatal("cgroup option accepted for immediate submit")
	}
}
