// Package supervisor executes trusted workloads with Linux process resource controls.
// These controls are not a sandbox for untrusted programs.
package supervisor

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"golang.org/x/sys/unix"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type Config struct {
	Executable, SHA256       string
	Args                     []string
	Input                    []byte
	MemoryMB                 int
	Timeout                  time.Duration
	StdoutLimit, StderrLimit int64
}
type Outcome struct {
	Stdout, Stderr []byte
	ExitCode       int
}

// HandleExec must be called before ordinary argument parsing, including in TestMain.
func HandleExec(args []string) bool {
	if len(args) == 0 || args[0] != "__exec" {
		return false
	}
	fail := func(e error) { fmt.Fprintln(os.Stderr, e); os.Exit(126) }
	if len(args) < 5 || args[1] != "--memory-mb" || args[3] != "--" {
		fail(errors.New("invalid exec helper arguments"))
		return true
	}
	mb, e := strconv.ParseUint(args[2], 10, 64)
	if e != nil || mb > ^uint64(0)/(1024*1024) {
		fail(errors.New("invalid memory limit"))
		return true
	}
	if mb > 0 {
		e = unix.Setrlimit(unix.RLIMIT_AS, &unix.Rlimit{Cur: mb * 1024 * 1024, Max: mb * 1024 * 1024})
		if e != nil {
			fail(e)
		}
	}
	// Keep fd 3 open across exec: scripts need their interpreter to reopen it.
	fail(unix.Exec("/proc/self/fd/3", args[4:], os.Environ()))
	return true
}
func minimalEnv() []string {
	env := []string{"PATH=/usr/bin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8"}
	for _, k := range []string{"TMPDIR", "LD_LIBRARY_PATH"} {
		if v, ok := os.LookupEnv(k); ok {
			env = append(env, k+"="+v)
		}
	}
	return env
}

type limitedBuffer struct {
	sync.Mutex
	b        bytes.Buffer
	limit    int64
	overflow chan struct{}
	once     sync.Once
}

func (b *limitedBuffer) Write(p []byte) (int, error) {
	b.Lock()
	defer b.Unlock()
	n := len(p)
	left := b.limit - int64(b.b.Len())
	if int64(n) > left {
		if left > 0 {
			b.b.Write(p[:int(left)])
		}
		b.once.Do(func() { close(b.overflow) })
		return n, nil
	}
	b.b.Write(p)
	return n, nil
}
func Run(ctx context.Context, c Config) (Outcome, error) {
	out := Outcome{ExitCode: -1}
	if !filepath.IsAbs(c.Executable) || c.MemoryMB < 0 || uint64(c.MemoryMB) > ^uint64(0)/(1024*1024) || c.Timeout <= 0 || c.StdoutLimit <= 0 || c.StderrLimit <= 0 {
		return out, errors.New("invalid supervisor configuration")
	}
	for _, s := range append([]string{c.Executable}, c.Args...) {
		if strings.ContainsRune(s, 0) {
			return out, errors.New("NUL in executable arguments")
		}
	}
	digest, e := hex.DecodeString(c.SHA256)
	if e != nil || len(digest) != sha256.Size {
		return out, errors.New("invalid executable SHA256")
	}
	// Nonblocking open lets the regular-file check reject FIFOs without waiting
	// for a writer before the child deadline can be established.
	f, e := os.OpenFile(c.Executable, os.O_RDONLY|unix.O_NONBLOCK, 0)
	if e != nil {
		return out, e
	}
	defer f.Close()
	st, e := f.Stat()
	if e != nil {
		return out, e
	}
	if !st.Mode().IsRegular() || st.Mode().Perm()&0111 == 0 {
		return out, errors.New("executable must be an executable regular file")
	}
	h := sha256.New()
	if _, e = io.Copy(h, f); e != nil {
		return out, e
	}
	if !bytes.Equal(h.Sum(nil), digest) {
		return out, errors.New("executable SHA256 mismatch")
	}
	self, e := os.Executable()
	if e != nil {
		return out, e
	}
	ctx, cancel := context.WithTimeout(ctx, c.Timeout)
	defer cancel()
	if e = ctx.Err(); e != nil {
		return out, e
	}
	args := []string{"__exec", "--memory-mb", strconv.Itoa(c.MemoryMB), "--", c.Executable}
	args = append(args, c.Args...)
	cmd := exec.Command(self, args...)
	cmd.Env = minimalEnv()
	cmd.ExtraFiles = []*os.File{f}
	cmd.Stdin = bytes.NewReader(c.Input)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	cmd.WaitDelay = 200 * time.Millisecond
	overflow := make(chan struct{})
	stdout := &limitedBuffer{limit: c.StdoutLimit, overflow: overflow}
	stderr := &limitedBuffer{limit: c.StderrLimit, overflow: make(chan struct{})}
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	// Linux associates Pdeathsig with the creating thread. Keep that thread alive
	// until Wait completes; otherwise Go retiring a thread can kill a healthy child.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if e = cmd.Start(); e != nil {
		return out, e
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	var cause error
	select {
	case e = <-done:
	case <-ctx.Done():
		cause = ctx.Err()
	case <-overflow:
		cause = errors.New("stdout limit exceeded")
	case <-stderr.overflow:
		cause = errors.New("stderr limit exceeded")
	}
	// Also kill descendants after a normally exiting leader, including pipe holders.
	_ = unix.Kill(-cmd.Process.Pid, unix.SIGKILL)
	if cause != nil {
		e = <-done
	}
	out.Stdout = stdout.b.Bytes()
	out.Stderr = stderr.b.Bytes()
	out.ExitCode = cmd.ProcessState.ExitCode()
	if cause != nil {
		return out, cause
	}
	select {
	case <-overflow:
		return out, errors.New("stdout limit exceeded")
	case <-stderr.overflow:
		return out, errors.New("stderr limit exceeded")
	default:
	}
	return out, e
}

// ErrIdentityMismatch means the live process does not identify as the requested worker.
// Callers must never signal that process as part of recovery.
var ErrIdentityMismatch = errors.New("worker identity mismatch")

func exitedPIDFD(fd int) (bool, error) {
	poll := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLIN}}
	for {
		_, err := unix.Poll(poll, 0)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil {
			return false, err
		}
		if poll[0].Revents&unix.POLLNVAL != 0 {
			return false, unix.EBADF
		}
		if poll[0].Revents&unix.POLLERR != 0 {
			return false, unix.EIO
		}
		return poll[0].Revents&(unix.POLLIN|unix.POLLHUP) != 0, nil
	}
}

func workerFD(pid int, jobID string) (int, error) {
	if pid <= 0 || jobID == "" || strings.ContainsRune(jobID, 0) {
		return -1, errors.New("invalid worker identity")
	}
	fd, e := unix.PidfdOpen(pid, 0)
	if e != nil {
		return -1, e
	}
	if exited, err := exitedPIDFD(fd); err != nil || exited {
		unix.Close(fd)
		if err != nil {
			return -1, err
		}
		return -1, unix.ESRCH
	}
	b, e := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
	if e != nil {
		unix.Close(fd)
		return -1, e
	}
	args := strings.Split(strings.TrimSuffix(string(b), "\x00"), "\x00")
	valid := false
	for i := 1; i+1 < len(args); i++ {
		if args[i] == "__worker" && args[i+1] == jobID {
			valid = true
			break
		}
	}
	if !valid {
		// Exit may race the first poll and leave an empty cmdline.
		exited, err := exitedPIDFD(fd)
		unix.Close(fd)
		if err != nil {
			return -1, err
		}
		if exited {
			return -1, unix.ESRCH
		}
		return -1, ErrIdentityMismatch
	}
	return fd, nil
}
func SignalWorker(pid int, jobID string, sig syscall.Signal) error {
	fd, e := workerFD(pid, jobID)
	if e != nil {
		return e
	}
	defer unix.Close(fd)
	return unix.PidfdSendSignal(fd, unix.Signal(sig), nil, 0)
}
func WorkerAlive(pid int, jobID string) (bool, error) {
	fd, e := workerFD(pid, jobID)
	if errors.Is(e, unix.ESRCH) || errors.Is(e, os.ErrNotExist) {
		return false, nil
	}
	if e != nil {
		return false, e
	}
	defer unix.Close(fd)
	e = unix.PidfdSendSignal(fd, 0, nil, 0)
	if errors.Is(e, unix.ESRCH) {
		return false, nil
	}
	return e == nil, e
}
