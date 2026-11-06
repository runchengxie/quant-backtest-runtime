package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"modernc.org/sqlite"
	sqlite3 "modernc.org/sqlite/lib"
)

// IsBusy recognizes primary and extended SQLite lock contention, never text.
func IsBusy(err error) bool {
	var sqlerr *sqlite.Error
	return errors.As(err, &sqlerr) && (sqlerr.Code()&255 == sqlite3.SQLITE_BUSY || sqlerr.Code()&255 == sqlite3.SQLITE_LOCKED)
}

// SetRetryContext is used only by the scheduler owner goroutine. An independent
// context preserves bounded cleanup writes while existing children drain.
func (s *Store) SetRetryContext(ctx context.Context) { s.retryContext = ctx }

// EndStartupRetry restores the normal worker policy before execution begins.
func (s *Store) EndStartupRetry() error {
	// Replace the pool as well as its active pragma: reconnects must retain the
	// normal driver defaults rather than the startup-only 100ms DSN.
	normal, err := Open(s.path, false)
	if err != nil {
		return err
	}
	previous := s.db
	s.db, s.retryContext = normal.db, nil
	return previous.Close()
}

func retry[T any](s *Store, parent context.Context, operation func(context.Context) (T, error)) (T, error) {
	if s.retryContext == nil {
		return operation(parent)
	}
	ctx, cancel := context.WithTimeout(parent, 35*time.Second)
	defer cancel()
	stop := context.AfterFunc(s.retryContext, cancel)
	defer stop()
	if s.retryContext.Err() != nil {
		cancel()
	}
	delay := 50 * time.Millisecond
	var lastBusy error
	var zero T
	for {
		if err := ctx.Err(); err != nil {
			if s.retryContext.Err() != nil {
				return zero, s.retryContext.Err()
			}
			if parent.Err() != nil {
				return zero, parent.Err()
			}
			if lastBusy == nil {
				return zero, err
			}
			return zero, fmt.Errorf("SQLite contention retry deadline exceeded: %w", lastBusy)
		}
		value, err := operation(ctx)
		if err == nil {
			return value, nil
		}
		if !IsBusy(err) {
			if ctx.Err() != nil && (errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded)) {
				continue
			}
			return value, err
		}
		lastBusy = err
		if ctx.Err() != nil {
			continue
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
		if delay < 200*time.Millisecond {
			delay *= 2
		}
	}
}
