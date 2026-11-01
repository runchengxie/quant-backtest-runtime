package jobgroup

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestRejectOrdinaryFilesystem(t *testing.T) {
	if _, err := New(t.TempDir(), "jobs.db"); err == nil {
		t.Fatal("ordinary filesystem accepted as enforcement")
	}
}
func TestRejectUnsafeIDs(t *testing.T) {
	m := &Manager{Path: t.TempDir()}
	for _, id := range []string{"", "..", "../escape", "a/b", "not-a-uuid"} {
		if _, err := m.Create(id, 1); err == nil {
			t.Fatalf("accepted %q", id)
		}
		if _, err := m.Inspect(id); err == nil {
			t.Fatalf("inspected %q", id)
		}
		if _, err := m.Cleanup(id); err == nil {
			t.Fatalf("cleaned %q", id)
		}
	}
}
func TestReadStats(t *testing.T) {
	dir := t.TempDir()
	for name, value := range map[string]string{"memory.max": "1048576\n", "memory.peak": "8192\n", "memory.events": "low 0\nhigh 1\nmax 3\noom 2\noom_kill 1\noom_group_kill 1\n", "cgroup.events": "populated 1\nfrozen 0\n"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
	}
	s, err := readStats(dir)
	if err != nil {
		t.Fatal(err)
	}
	if s.MemoryMax != 1048576 || s.MemoryPeak != 8192 || s.OOMKill != 1 || !s.Populated || s.Events["oom"] != 2 {
		t.Fatalf("bad stats: %+v", s)
	}
}

// Run inside systemd-run --user -p Delegate=memory -p
// DelegateSubgroup=supervisor with JOBGROUP_INTEGRATION=1.
func TestDelegatedLifecycle(t *testing.T) {
	if os.Getenv("JOBGROUP_INTEGRATION") != "1" {
		t.Skip("requires real delegated cgroup v2")
	}
	m, err := New("auto", filepath.Join(t.TempDir(), "jobs.db"))
	if err != nil {
		t.Fatal(err)
	}
	id := "1fb6a53c-7898-487f-a164-d6ae3b1e818b"
	fd, err := m.Create(id, 16)
	if err != nil {
		t.Fatal(err)
	}
	defer fd.Close()
	defer func() {
		if _, err := m.Cleanup(id); err != nil {
			t.Error(err)
		}
	}()
	if _, err = m.Create(id, 16); err == nil {
		t.Fatal("reused existing cgroup")
	}
	s, err := m.Inspect(id)
	if err != nil {
		t.Fatal(err)
	}
	if s.MemoryMax != 16*1024*1024 || s.Populated {
		t.Fatalf("unexpected %+v", s)
	}
	cmd := exec.Command("/bin/sh", "-c", "sleep 60 & wait")
	cmd.SysProcAttr = &syscall.SysProcAttr{UseCgroupFD: true, CgroupFD: int(fd.Fd())}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	s, err = m.Inspect(id)
	if err != nil || !s.Populated {
		t.Fatalf("child not atomically placed: %+v %v", s, err)
	}
	if _, err := m.Cleanup(id); err != nil {
		t.Fatal(err)
	}
	_ = cmd.Wait()
	if _, err := m.Inspect(id); !os.IsNotExist(err) {
		t.Fatalf("group remains: %v", err)
	}
	fd.Close()
	fd, err = m.Create(id, 16)
	if err != nil {
		t.Fatal(err)
	}
	defer fd.Close()
	ids, err := m.List()
	if err != nil || len(ids) != 1 || ids[0] != id {
		t.Fatalf("list %v: %v", ids, err)
	}
	for name, want := range map[string]string{"memory.swap.max": "0", "memory.oom.group": "1"} {
		b, err := os.ReadFile(filepath.Join(m.Path, id, name))
		if err != nil || strings.TrimSpace(string(b)) != want {
			t.Fatalf("%s=%q: %v", name, b, err)
		}
	}
}
