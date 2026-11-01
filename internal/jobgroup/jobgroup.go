// Package jobgroup enforces aggregate Linux cgroup v2 memory limits per job.
package jobgroup

import (
	"crypto/sha256"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"golang.org/x/sys/unix"
)

type Manager struct{ Path string }
type Stats struct {
	MemoryMax  uint64            `json:"memory_max"`
	MemoryPeak uint64            `json:"memory_peak"`
	OOMKill    uint64            `json:"oom_kill"`
	Events     map[string]uint64 `json:"memory_events"`
	Populated  bool              `json:"populated"`
}

// New requires an empty, writable delegated cgroup v2 parent. Auto discovery
// only accepts a systemd DelegateSubgroup named supervisor.
func New(root, database string) (*Manager, error) {
	if root == "auto" {
		b, err := os.ReadFile("/proc/self/cgroup")
		if err != nil {
			return nil, err
		}
		var rel string
		for _, line := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(line, "0::") {
				rel = strings.TrimPrefix(line, "0::")
			}
		}
		if filepath.Base(rel) != "supervisor" || !filepath.IsAbs(rel) || strings.Contains(rel, "..") {
			return nil, fmt.Errorf("auto cgroup requires systemd DelegateSubgroup=supervisor")
		}
		root = filepath.Join("/sys/fs/cgroup", filepath.Dir(rel))
	}
	if !filepath.IsAbs(root) {
		return nil, fmt.Errorf("cgroup root must be absolute")
	}
	resolved, err := filepath.EvalSymlinks(root)
	if err != nil {
		return nil, err
	}
	if resolved != filepath.Clean(root) {
		return nil, fmt.Errorf("cgroup root must not contain symlinks")
	}
	var fs unix.Statfs_t
	if err = unix.Statfs(root, &fs); err != nil {
		return nil, err
	}
	if fs.Type != unix.CGROUP2_SUPER_MAGIC {
		return nil, fmt.Errorf("%s is not cgroup v2", root)
	}
	if err = enableMemory(root); err != nil {
		return nil, err
	}
	abs, err := filepath.Abs(database)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256([]byte(abs))
	path := filepath.Join(root, fmt.Sprintf("backtest-jobs-%x", digest[:16]))
	if err = os.Mkdir(path, 0700); err != nil && !os.IsExist(err) {
		return nil, err
	}
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, fmt.Errorf("invalid namespace")
	}
	if err = enableMemory(path); err != nil {
		return nil, err
	}
	return &Manager{Path: path}, nil
}
func enableMemory(path string) error {
	b, err := os.ReadFile(filepath.Join(path, "cgroup.procs"))
	if err != nil {
		return err
	}
	if strings.TrimSpace(string(b)) != "" {
		return fmt.Errorf("delegated parent %s has processes", path)
	}
	b, err = os.ReadFile(filepath.Join(path, "cgroup.controllers"))
	if err != nil {
		return err
	}
	found := false
	for _, v := range strings.Fields(string(b)) {
		if v == "memory" {
			found = true
		}
	}
	if !found {
		return fmt.Errorf("memory controller not delegated at %s", path)
	}
	return os.WriteFile(filepath.Join(path, "cgroup.subtree_control"), []byte("+memory"), 0600)
}
func validID(id string) bool { u, err := uuid.Parse(id); return err == nil && u.String() == id }
func (m *Manager) jobPath(id string) (string, error) {
	if !validID(id) {
		return "", fmt.Errorf("invalid job UUID %q", id)
	}
	p := filepath.Join(m.Path, id)
	info, err := os.Lstat(p)
	if err != nil {
		return "", err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("invalid job cgroup")
	}
	return p, nil
}

// Create creates a fresh group and returns a directory descriptor suitable for
// SysProcAttr.UseCgroupFD. The caller closes the descriptor after Start.
func (m *Manager) Create(id string, memoryMB int) (*os.File, error) {
	if !validID(id) {
		return nil, fmt.Errorf("invalid job UUID %q", id)
	}
	if memoryMB <= 0 || uint64(memoryMB) > math.MaxInt64/(1024*1024) {
		return nil, fmt.Errorf("invalid memory limit")
	}
	p := filepath.Join(m.Path, id)
	if err := os.Mkdir(p, 0700); err != nil {
		return nil, err
	}
	for _, setting := range [][2]string{{"memory.max", strconv.FormatUint(uint64(memoryMB)*1024*1024, 10)}, {"memory.swap.max", "0"}, {"memory.oom.group", "1"}} {
		if err := os.WriteFile(filepath.Join(p, setting[0]), []byte(setting[1]), 0600); err != nil {
			_ = os.Remove(p)
			return nil, err
		}
	}
	f, err := os.Open(p)
	if err != nil {
		_ = os.Remove(p)
	}
	return f, err
}
func (m *Manager) Inspect(id string) (Stats, error) {
	p, err := m.jobPath(id)
	if err != nil {
		return Stats{}, err
	}
	return readStats(p)
}
func (m *Manager) List() ([]string, error) {
	entries, err := os.ReadDir(m.Path)
	if err != nil {
		return nil, err
	}
	ids := []string{}
	for _, e := range entries {
		if e.IsDir() && validID(e.Name()) {
			ids = append(ids, e.Name())
		}
	}
	sort.Strings(ids)
	return ids, nil
}

// Cleanup kills all descendants, captures final counters, and removes only the
// job cgroup. Failure leaves the group available for subsequent recovery.
func (m *Manager) Cleanup(id string) (Stats, error) {
	p, err := m.jobPath(id)
	if err != nil {
		return Stats{}, err
	}
	if err = os.WriteFile(filepath.Join(p, "cgroup.kill"), []byte("1"), 0600); err != nil {
		return Stats{}, err
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		s, err := readStats(p)
		if err != nil {
			return s, err
		}
		if !s.Populated {
			return s, os.Remove(p)
		}
		if time.Now().After(deadline) {
			return s, fmt.Errorf("cgroup %s remains populated", id)
		}
		time.Sleep(10 * time.Millisecond)
	}
}
func readStats(path string) (Stats, error) {
	s := Stats{Events: map[string]uint64{}}
	for _, field := range []struct {
		name   string
		target *uint64
	}{{"memory.max", &s.MemoryMax}, {"memory.peak", &s.MemoryPeak}} {
		b, err := os.ReadFile(filepath.Join(path, field.name))
		if err != nil {
			return s, err
		}
		value, err := strconv.ParseUint(strings.TrimSpace(string(b)), 10, 64)
		if err != nil {
			return s, fmt.Errorf("%s: %w", field.name, err)
		}
		*field.target = value
	}
	for _, name := range []string{"memory.events", "cgroup.events"} {
		b, err := os.ReadFile(filepath.Join(path, name))
		if err != nil {
			return s, err
		}
		for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
			fields := strings.Fields(line)
			if len(fields) != 2 {
				return s, fmt.Errorf("malformed %s", name)
			}
			value, err := strconv.ParseUint(fields[1], 10, 64)
			if err != nil {
				return s, err
			}
			if name == "memory.events" {
				s.Events[fields[0]] = value
			} else if fields[0] == "populated" {
				s.Populated = value != 0
			}
		}
	}
	s.OOMKill = s.Events["oom_kill"]
	return s, nil
}
