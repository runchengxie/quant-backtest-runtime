package store

// Active returns jobs whose claimed worker may still own resources.
func (s *Store) Active() ([]Job, error) {
	return s.list("WHERE status='RUNNING' ORDER BY created_at,job_id")
}

// Unpublished permits idempotent cleanup after a terminal-state commit interrupted cleanup.
func (s *Store) Unpublished() ([]Job, error) {
	return s.list("WHERE status IN ('FAILED','CANCELLED') ORDER BY created_at,job_id")
}
