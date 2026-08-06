package store

import (
	"context"
	_ "embed"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed migrations/0001_hosted_contracts.up.sql
var initialMigration string

type Store struct {
	pool *pgxpool.Pool
}

func Open(ctx context.Context, databaseURL string) (*Store, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("open PostgreSQL pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping PostgreSQL: %w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close() {
	s.pool.Close()
}

func (s *Store) Migrate(ctx context.Context) error {
	if _, err := s.pool.Exec(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
        version integer PRIMARY KEY,
        applied_at timestamptz NOT NULL DEFAULT transaction_timestamp()
    )`); err != nil {
		return fmt.Errorf("create migration ledger: %w", err)
	}
	var applied bool
	if err := s.pool.QueryRow(ctx, "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = 1)").Scan(&applied); err != nil {
		return fmt.Errorf("read migration ledger: %w", err)
	}
	if applied {
		return nil
	}
	if _, err := s.pool.Exec(ctx, initialMigration); err != nil {
		return fmt.Errorf("apply hosted schema migration 1: %w", err)
	}
	if _, err := s.pool.Exec(ctx, "INSERT INTO schema_migrations (version) VALUES (1)"); err != nil {
		return fmt.Errorf("record migration 1: %w", err)
	}
	return nil
}

var ErrNotFound = errors.New("not found")
