package app

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/runchengxie/quant-backtest-runtime/internal/jobgroup"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
	"github.com/runchengxie/quant-backtest-runtime/internal/supervisor"
)

func finishGroup(o Options, id string) error {
	if o.groups == nil {
		return nil
	}
	stats, err := o.groups.Inspect(id)
	if err != nil {
		return err
	}
	// Preserve a synchronized snapshot before removing the kernel counters.
	if err = writeResources(o, id, stats, false); err != nil {
		return err
	}
	stats, err = o.groups.Cleanup(id)
	if err != nil {
		return err
	}
	return writeResources(o, id, stats, true)
}

func writeResources(o Options, id string, stats jobgroup.Stats, complete bool) error {
	root := filepath.Join(o.ResultRoot, ".logs")
	if err := os.MkdirAll(root, 0700); err != nil {
		return err
	}
	data, err := json.Marshal(map[string]any{"schema": "quant.job_resources.v1", "job_id": id, "scope": "worker_and_descendants", "cleanup_complete": complete, "cgroup": stats})
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(root, ".resources-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(append(data, '\n')); err != nil {
		f.Close()
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = os.Rename(f.Name(), filepath.Join(root, id+".resources.json")); err != nil {
		return err
	}
	return syncDirectory(root)
}

// Reconcile groups left by an earlier scheduler without signalling an unrelated
// reused PID. A live claimed worker keeps its slot through its actual exit.
func reconcileGroups(o Options, st *store.Store, running map[string]*exec.Cmd, observed map[string]int) error {
	if o.groups == nil {
		return nil
	}
	ids, err := o.groups.List()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, owned := running[id]; owned {
			continue
		}
		job, err := st.Get(id)
		if err != nil {
			return err
		}
		if job.WorkerPID != nil {
			alive, err := supervisor.WorkerAlive(*job.WorkerPID, id)
			if err != nil && !errors.Is(err, supervisor.ErrIdentityMismatch) {
				return err
			}
			if alive {
				observed[id] = *job.WorkerPID
				st.SetRetryContext(context.Background())
				continue
			}
		}
		stats, err := o.groups.Inspect(id)
		if err != nil {
			return err
		}
		if job.Status == "SUBMITTED" || job.Status == "RUNNING" {
			code := "WORKER_EXITED"
			if stats.OOMKill > 0 {
				code = "JOB_MEMORY_EXCEEDED"
			}
			if err = failAbandoned(o, st, job, code); err != nil {
				return err
			}
		}
		if err = finishGroup(o, id); err != nil {
			return err
		}
	}
	return reconcileMissingGroups(o, st)
}

func failAbandoned(o Options, st *store.Store, job store.Job, code string) error {
	state := "FAILED"
	if job.CancelRequestedAt != nil {
		state, code = "CANCELLED", ""
	}
	done, err := st.Finish(job.JobID, state, "", "", code, "worker exited before scheduler reconciliation")
	if err != nil {
		return err
	}
	if done {
		removeOutput(o, job.JobID)
	}
	return nil
}

// systemd may remove the delegated tree when the service dies. Reconcile dead
// claimed workers even when their old kernel counters have already disappeared.
func reconcileMissingGroups(o Options, st *store.Store) error {
	active, err := st.Active()
	if err != nil {
		return err
	}
	queued, err := st.Queued()
	if err != nil {
		return err
	}
	for _, job := range append(active, queued...) {
		if job.LeaseExpiresAt == nil {
			continue
		}
		if _, err = o.groups.Inspect(job.JobID); err == nil {
			continue
		} else if !os.IsNotExist(err) {
			return err
		}
		if job.WorkerPID != nil {
			alive, err := supervisor.WorkerAlive(*job.WorkerPID, job.JobID)
			if err != nil && !errors.Is(err, supervisor.ErrIdentityMismatch) {
				return err
			}
			if alive {
				return failure("RESOURCE_LIMIT_UNAVAILABLE", "live worker has no job cgroup; quiesce previous manager before cutover")
			}
		}
		if err = failAbandoned(o, st, job, "WORKER_EXITED"); err != nil {
			return err
		}
	}
	return nil
}
