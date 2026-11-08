package store

import (
	"context"
	"fmt"
)

const checkpointSchema = `
CREATE TABLE IF NOT EXISTS financial_attempts(job_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS financial_checkpoints(job_id TEXT NOT NULL, token TEXT NOT NULL, binding TEXT NOT NULL, cursor INTEGER NOT NULL CHECK(cursor>0), digest TEXT NOT NULL, PRIMARY KEY(job_id,cursor));`

// Checkpoints are immutable admissions, never inferred from filesystem contents.
type Checkpoint struct {
	JobID   string `json:"job_id"`
	Token   string `json:"token"`
	Binding string `json:"binding"`
	Cursor  int    `json:"cursor"`
	Digest  string `json:"digest"`
}

func (s *Store) BeginAttempt(id, token string) error {
	if token == "" {
		return fmt.Errorf("empty attempt token")
	}
	ok, e := changed(s.db.Exec(`INSERT INTO financial_attempts(job_id,token) SELECT job_id,? FROM backtest_jobs WHERE job_id=? AND status='RUNNING' AND cancel_requested_at IS NULL`, token, id))
	if e == nil && !ok {
		e = ErrConflict
	}
	return e
}

func (s *Store) AdmitCheckpoint(c Checkpoint) (bool, error) {
	if c.Cursor <= 0 || !digestPattern.MatchString(c.Digest) || c.Binding == "" {
		return false, fmt.Errorf("invalid checkpoint receipt")
	}
	return changed(s.db.Exec(`INSERT INTO financial_checkpoints(job_id,token,binding,cursor,digest)
 SELECT j.job_id,a.token,?,?,? FROM backtest_jobs j JOIN financial_attempts a USING(job_id)
 WHERE j.job_id=? AND a.token=? AND j.status='RUNNING' AND j.cancel_requested_at IS NULL
 AND NOT EXISTS(SELECT 1 FROM financial_checkpoints WHERE job_id=j.job_id AND (cursor>=? OR binding<>?))`, c.Binding, c.Cursor, c.Digest, c.JobID, c.Token, c.Cursor, c.Binding))
}

func (s *Store) ResumeCheckpoint(id, binding string) (Checkpoint, error) {
	var c Checkpoint
	e := s.db.QueryRow(`SELECT c.job_id,c.token,c.binding,c.cursor,c.digest FROM financial_checkpoints c JOIN backtest_jobs j USING(job_id) JOIN financial_attempts a USING(job_id)
 WHERE c.job_id=? AND c.binding=? AND c.token=a.token AND j.status='FAILED' AND j.cancel_requested_at IS NULL ORDER BY c.cursor DESC LIMIT 1`, id, binding).Scan(&c.JobID, &c.Token, &c.Binding, &c.Cursor, &c.Digest)
	if e != nil {
		return c, fmt.Errorf("resume requires a failed predecessor with an admitted compatible checkpoint: %w", e)
	}
	return c, nil
}

// A job is never reclaimed; a new successor gets a distinct job ID and token.
func (s *Store) FinishAttempt(ctx context.Context, id, token, ref, digest string) (bool, error) {
	if ref != "backtest-job://"+id || !digestPattern.MatchString(digest) {
		return false, fmt.Errorf("invalid result identity")
	}
	return changed(s.db.ExecContext(ctx, `UPDATE backtest_jobs SET status='SUCCEEDED',updated_at=?,worker_pid=NULL,lease_expires_at=NULL,result_ref=?,result_sha256=? WHERE job_id=? AND status='RUNNING' AND cancel_requested_at IS NULL AND EXISTS(SELECT 1 FROM financial_attempts WHERE job_id=? AND token=?)`, now(), ref, digest, id, id, token))
}
