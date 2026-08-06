package store

import (
	"context"
	"embed"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed migrations/*.up.sql
var migrationFiles embed.FS

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

func (s *Store) Pool() *pgxpool.Pool {
	return s.pool
}

func (s *Store) Migrate(ctx context.Context) error {
	if _, err := s.pool.Exec(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
        version integer PRIMARY KEY,
        applied_at timestamptz NOT NULL DEFAULT transaction_timestamp()
    )`); err != nil {
		return fmt.Errorf("create migration ledger: %w", err)
	}
	entries, err := migrationFiles.ReadDir("migrations")
	if err != nil {
		return fmt.Errorf("read embedded migrations: %w", err)
	}
	versions := make([]int, 0, len(entries))
	byVersion := make(map[int]string, len(entries))
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		versionText, _, found := strings.Cut(entry.Name(), "_")
		version, parseErr := strconv.Atoi(versionText)
		if !found || parseErr != nil {
			return fmt.Errorf("invalid migration file %q", entry.Name())
		}
		contents, readErr := migrationFiles.ReadFile("migrations/" + entry.Name())
		if readErr != nil {
			return fmt.Errorf("read migration %d: %w", version, readErr)
		}
		versions = append(versions, version)
		byVersion[version] = string(contents)
	}
	sort.Ints(versions)
	for _, version := range versions {
		var applied bool
		if err := s.pool.QueryRow(ctx, "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)", version).Scan(&applied); err != nil {
			return fmt.Errorf("read migration ledger: %w", err)
		}
		if applied {
			continue
		}
		if _, err := s.pool.Exec(ctx, byVersion[version]); err != nil {
			return fmt.Errorf("apply hosted schema migration %d: %w", version, err)
		}
		if _, err := s.pool.Exec(ctx, "INSERT INTO schema_migrations (version) VALUES ($1)", version); err != nil {
			return fmt.Errorf("record migration %d: %w", version, err)
		}
	}
	return nil
}

var ErrNotFound = errors.New("not found")
