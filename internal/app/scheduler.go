package app

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"syscall"
	"time"

	"github.com/runchengxie/quant-backtest-runtime/internal/jobgroup"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
	"github.com/runchengxie/quant-backtest-runtime/internal/supervisor"
	"golang.org/x/sys/unix"
)

func schedulerLock(path string, exclusive bool) (*os.File, error) {
	f, e := os.OpenFile(path+".scheduler.lock", os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	mode := unix.LOCK_SH | unix.LOCK_NB
	if exclusive {
		mode = unix.LOCK_EX | unix.LOCK_NB
	}
	if e = unix.Flock(int(f.Fd()), mode); e != nil {
		f.Close()
		return nil, failure("SCHEDULER_MODE_CONFLICT", "another scheduler owns this database; use submit --queue")
	}
	return f, nil
}
func schedulerActive(path string) bool {
	f, e := os.OpenFile(path+".scheduler.lock", os.O_RDWR, 0600)
	if e != nil {
		return false
	}
	defer f.Close()
	if e = unix.Flock(int(f.Fd()), unix.LOCK_SH|unix.LOCK_NB); e != nil {
		return true
	}
	_ = unix.Flock(int(f.Fd()), unix.LOCK_UN)
	return false
}

type completion struct {
	id  string
	err error
}

func serve(ctx context.Context, o Options) (resultErr error) {
	defer func() {
		if errors.Is(resultErr, context.Canceled) && ctx.Err() != nil {
			resultErr = nil
		}
	}()
	if o.CgroupRoot != "" {
		var err error
		o.groups, err = jobgroup.New(o.CgroupRoot, o.Database)
		if err != nil {
			return failure("RESOURCE_LIMIT_UNAVAILABLE", err.Error())
		}
	}
	st, e := store.OpenScheduler(ctx, o.Database)
	if e != nil {
		return e
	}
	defer st.Close()
	lock, e := schedulerLock(o.Database, true)
	if e != nil {
		return e
	}
	defer lock.Close()
	done := make(chan completion, o.Concurrency)
	running := map[string]*exec.Cmd{}
	defer func() {
		if len(running) == 0 {
			return
		}
		for id, command := range running {
			_ = supervisor.SignalWorker(command.Process.Pid, id, syscall.SIGINT)
		}
		timer := time.NewTimer(2 * time.Second)
		defer timer.Stop()
		for len(running) > 0 {
			select {
			case result := <-done:
				delete(running, result.id)
			case <-timer.C:
				for _, command := range running {
					_ = command.Process.Kill()
				}
				for len(running) > 0 {
					result := <-done
					delete(running, result.id)
				}
			}
		}
	}()
	observed := map[string]int{}
	stopping := false
	for {
		if ctx.Err() != nil {
			stopping = true
			if len(running) == 0 && len(observed) == 0 {
				return nil
			}
		}
		if len(running) > 0 || len(observed) > 0 {
			st.SetRetryContext(context.Background())
		} else {
			st.SetRetryContext(ctx)
		}
		if e = reconcileGroups(o, st, running, observed); e != nil {
			return e
		}
		if _, e = recoverJobs(st, o); e != nil {
			return e
		}
		drain := true
		for drain {
			select {
			case result := <-done:
				delete(running, result.id)
				j, err := st.Get(result.id)
				if err != nil {
					return err
				}
				if j.Status == "RUNNING" || j.Status == "SUBMITTED" {
					code := "WORKER_EXITED"
					if o.groups != nil {
						stats, err := o.groups.Inspect(j.JobID)
						if err != nil {
							return err
						}
						if stats.OOMKill > 0 {
							code = "JOB_MEMORY_EXCEEDED"
						}
					}
					state := "FAILED"
					if j.CancelRequestedAt != nil {
						state = "CANCELLED"
					}
					_, err = st.Finish(j.JobID, state, "", "", code, "worker exited without completing state")
					if err != nil {
						return err
					}
					removeOutput(o, j.JobID)
				}
				if err = finishGroup(o, j.JobID); err != nil {
					return err
				}
			default:
				drain = false
			}
		}
		active, e := st.Active()
		if e != nil {
			return e
		}
		slots := len(running)
		for _, j := range active {
			if _, owned := running[j.JobID]; !owned && j.WorkerPID != nil {
				observed[j.JobID] = *j.WorkerPID
				st.SetRetryContext(context.Background())
			}
		}
		for id, pid := range observed {
			alive, err := supervisor.WorkerAlive(pid, id)
			if errors.Is(err, supervisor.ErrIdentityMismatch) {
				alive = false
				err = nil
			}
			if err != nil || alive {
				slots++
			} else {
				delete(observed, id)
			}
		}
		queued, e := st.Queued()
		if e != nil {
			return e
		}
		for _, j := range queued {
			if _, owned := running[j.JobID]; !owned && j.LeaseExpiresAt != nil {
				slots++
			}
		}
		if (o.Once || stopping) && len(queued) == 0 && slots == 0 {
			return nil
		}
		if stopping && slots == 0 {
			return nil
		}
		if !stopping {
			for _, j := range queued {
				if slots >= o.Concurrency {
					break
				}
				if _, owned := running[j.JobID]; owned || j.LeaseExpiresAt != nil {
					continue
				}
				if ctx.Err() != nil {
					break
				}
				command, err := startWorker(o, st, j.JobID)
				if err != nil {
					if store.IsBusy(err) || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
						return err
					}
					code := "WORKER_START_FAILED"
					var typed *Error
					if errors.As(err, &typed) {
						code = typed.Code
					}
					if _, finishErr := st.Finish(j.JobID, "FAILED", "", "", code, err.Error()); finishErr != nil {
						return finishErr
					}
					continue
				}
				if command == nil {
					continue
				}
				running[j.JobID] = command
				st.SetRetryContext(context.Background())
				slots++
				go func(id string, command *exec.Cmd) { done <- completion{id, command.Wait()} }(j.JobID, command)
			}
		}
		if stopping {
			time.Sleep(50 * time.Millisecond)
			continue
		}
		timer := time.NewTimer(50 * time.Millisecond)
		select {
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
		}
	}
}
