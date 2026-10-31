package supervisor

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestMain(m *testing.M) {
	if HandleExec(os.Args[1:]) {
		return
	}
	os.Exit(m.Run())
}
func shellConfig(t *testing.T, script string) Config {
	t.Helper()
	b, e := os.ReadFile("/bin/sh")
	if e != nil {
		t.Fatal(e)
	}
	return Config{Executable: "/bin/sh", SHA256: fmt.Sprintf("%x", sha256.Sum256(b)), Args: []string{"-c", script}, Timeout: time.Second, StdoutLimit: 1024, StderrLimit: 1024}
}
func TestRunSuccessAndFailure(t *testing.T) {
	for _, n := range []int{0, 7} {
		c := shellConfig(t, fmt.Sprintf("printf hello; printf error >&2; exit %d", n))
		o, e := Run(context.Background(), c)
		if o.ExitCode != n || string(o.Stdout) != "hello" || string(o.Stderr) != "error" || (n == 0 && e != nil) {
			t.Fatalf("%+v %v", o, e)
		}
	}
}
func TestRunTimeout(t *testing.T) {
	c := shellConfig(t, "sleep 20 & wait")
	c.Timeout = 100 * time.Millisecond
	start := time.Now()
	_, e := Run(context.Background(), c)
	if e == nil || time.Since(start) > 2*time.Second {
		t.Fatalf("timeout: %v", e)
	}
}
func TestRunOverflow(t *testing.T) {
	c := shellConfig(t, "while :; do printf 1234567890; done")
	o, e := Run(context.Background(), c)
	if e == nil || len(o.Stdout) > 1024 {
		t.Fatalf("overflow %d %v", len(o.Stdout), e)
	}
}
func TestValidation(t *testing.T) {
	c := shellConfig(t, "touch /should-never-exist")
	c.SHA256 = "bad"
	if _, e := Run(context.Background(), c); e == nil {
		t.Fatal("accepted digest")
	}
}
func TestCancel(t *testing.T) {
	c := shellConfig(t, "sleep 20")
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(50*time.Millisecond, cancel)
	_, e := Run(ctx, c)
	if e == nil {
		t.Fatal("cancel accepted as success")
	}
}
func TestIdentityMismatch(t *testing.T) {
	if _, e := WorkerAlive(os.Getpid(), "unrelated"); !errors.Is(e, ErrIdentityMismatch) {
		t.Fatal("accepted unrelated process")
	}
	if e := SignalWorker(os.Getpid(), "unrelated", 0); !errors.Is(e, ErrIdentityMismatch) {
		t.Fatal("signalled unrelated process")
	}
}
func TestStderrOverflow(t *testing.T) {
	c := shellConfig(t, "while :; do printf 1234567890 >&2; done")
	o, e := Run(context.Background(), c)
	if e == nil || len(o.Stderr) > 1024 {
		t.Fatalf("overflow %d %v", len(o.Stderr), e)
	}
}
func TestMinimalEnvironment(t *testing.T) {
	t.Setenv("SUPERVISOR_SECRET", "hidden")
	c := shellConfig(t, "printf '%s' \"${SUPERVISOR_SECRET-unset}\"")
	o, e := Run(context.Background(), c)
	if e != nil || string(o.Stdout) != "unset" {
		t.Fatalf("%s %v", o.Stdout, e)
	}
}
func TestMemoryLimit(t *testing.T) {
	c := shellConfig(t, "ulimit -v")
	c.MemoryMB = 128
	o, e := Run(context.Background(), c)
	if e != nil || string(o.Stdout) != "131072\n" {
		t.Fatalf("%s %v", o.Stdout, e)
	}
}

func TestParentDeath(t *testing.T) {
	if path := os.Getenv("SUPERVISOR_PARENT_TEST"); path != "" {
		c := shellConfig(t, "echo $$ > "+path+"; exec sleep 30")
		c.Timeout = time.Minute
		_, _ = Run(context.Background(), c)
		return
	}
	path := filepath.Join(t.TempDir(), "pid")
	cmd := exec.Command(os.Args[0], "-test.run=^TestParentDeath$")
	cmd.Env = append(os.Environ(), "SUPERVISOR_PARENT_TEST="+path)
	if e := cmd.Start(); e != nil {
		t.Fatal(e)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	var pid int
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if b, e := os.ReadFile(path); e == nil {
			fmt.Sscanf(string(b), "%d", &pid)
			if pid > 0 {
				break
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	if pid == 0 {
		t.Fatal("child did not start")
	}
	defer syscall.Kill(pid, syscall.SIGKILL)
	if e := cmd.Process.Kill(); e != nil {
		t.Fatal(e)
	}
	for time.Now().Before(deadline) {
		b, e := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
		if os.IsNotExist(e) || (e == nil && strings.Contains(string(b), ") Z ")) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("child survived parent death")
}
func TestDescendantPipeCleanup(t *testing.T) {
	c := shellConfig(t, "sleep 20 & exit 0")
	start := time.Now()
	_, _ = Run(context.Background(), c)
	if time.Since(start) > time.Second {
		t.Fatal("descendant held pipe open")
	}
}
func TestHeldExecutableAgainstReplacement(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "program")
	a := filepath.Join(dir, "a")
	b := filepath.Join(dir, "b")
	bytes, e := os.ReadFile("/bin/sh")
	if e != nil {
		t.Fatal(e)
	}
	if e = os.WriteFile(a, bytes, 0700); e != nil {
		t.Fatal(e)
	}
	bad, e := os.ReadFile("/usr/bin/false")
	if e != nil {
		t.Fatal(e)
	}
	if e = os.WriteFile(b, bad, 0700); e != nil {
		t.Fatal(e)
	}
	if e = os.Symlink(a, path); e != nil {
		t.Fatal(e)
	}
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			select {
			case <-stop:
				return
			default:
			}
			for _, target := range []string{a, b} {
				_ = os.Remove(path + ".next")
				_ = os.Symlink(target, path+".next")
				_ = os.Rename(path+".next", path)
			}
		}
	}()
	defer func() { close(stop); <-done }()
	c := shellConfig(t, "printf verified")
	c.Executable = path
	for i := 0; i < 30; i++ {
		o, e := Run(context.Background(), c)
		if e == nil {
			if string(o.Stdout) != "verified" {
				t.Fatalf("unexpected executable: %+v", o)
			}
		} else if !strings.Contains(e.Error(), "SHA256 mismatch") {
			t.Fatalf("executed replacement: %+v %v", o, e)
		}
	}
}
func TestWorkerProcess(t *testing.T) {
	if os.Getenv("SUPERVISOR_WORKER_TEST") == "1" {
		time.Sleep(30 * time.Second)
	}
}
func TestWorkerIdentityAndSignal(t *testing.T) {
	cmd := exec.Command(os.Args[0], "-test.run=^TestWorkerProcess$", "--", "__worker", "owned-job")
	cmd.Env = append(os.Environ(), "SUPERVISOR_WORKER_TEST=1")
	if e := cmd.Start(); e != nil {
		t.Fatal(e)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	if alive, e := WorkerAlive(cmd.Process.Pid, "owned-job"); e != nil || !alive {
		t.Fatalf("alive %t %v", alive, e)
	}
	if e := SignalWorker(cmd.Process.Pid, "owned-job", syscall.SIGKILL); e != nil {
		t.Fatal(e)
	}
}

func TestKilledUnreapedWorkerIsNotAlive(t *testing.T) {
	cmd := exec.Command(os.Args[0], "-test.run=^TestWorkerProcess$", "--", "__worker", "owned-zombie")
	cmd.Env = append(os.Environ(), "SUPERVISOR_WORKER_TEST=1")
	if e := cmd.Start(); e != nil {
		t.Fatal(e)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	if e := cmd.Process.Kill(); e != nil {
		t.Fatal(e)
	}
	// Observe zombie state without reaping: its /proc cmdline is now empty.
	deadline := time.Now().Add(time.Second)
	for {
		b, e := os.ReadFile(fmt.Sprintf("/proc/%d/stat", cmd.Process.Pid))
		if e != nil {
			t.Fatal(e)
		}
		if strings.Contains(string(b), ") Z ") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("child did not exit")
		}
		time.Sleep(time.Millisecond)
	}
	if alive, e := WorkerAlive(cmd.Process.Pid, "owned-zombie"); alive || e != nil {
		t.Fatalf("unreaped worker alive=%t err=%v", alive, e)
	}
}

func TestFIFOExecutableFailsPromptly(t *testing.T) {
	if fifo := os.Getenv("SUPERVISOR_FIFO_TEST"); fifo != "" {
		c := shellConfig(t, "exit 0")
		c.Executable = fifo
		c.Timeout = 100 * time.Millisecond
		if _, e := Run(context.Background(), c); e == nil {
			t.Fatal("accepted FIFO executable")
		}
		return
	}
	fifo := filepath.Join(t.TempDir(), "program.fifo")
	if e := syscall.Mkfifo(fifo, 0600); e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestFIFOExecutableFailsPromptly$")
	cmd.Env = append(os.Environ(), "SUPERVISOR_FIFO_TEST="+fifo)
	output, e := cmd.CombinedOutput()
	if ctx.Err() != nil {
		t.Fatal("FIFO open blocked beyond supervisor deadline")
	}
	if e != nil {
		t.Fatalf("child: %v %s", e, output)
	}
}
