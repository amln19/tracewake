package store

import (
	"context"
	"embed"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed migrations/*.up.sql
var migrationFiles embed.FS

const migrationStatementBreak = "-- tracewake-statement-break"
const migrationLockID int64 = 0x747261636577616b

func migrationStatements(contents string) []string {
	// Deployed migration bytes retain the pre-rename delimiter. Normalizing it
	// here preserves those bytes without making it the name for new migrations.
	contents = strings.ReplaceAll(contents, "-- locus-statement-break", migrationStatementBreak)
	return strings.Split(contents, migrationStatementBreak)
}

func migrationTransactionBody(statement string) (string, error) {
	statement = strings.TrimSpace(statement)
	upper := strings.ToUpper(statement)
	if strings.HasPrefix(upper, "BEGIN;") {
		statement = strings.TrimSpace(statement[len("BEGIN;"):])
		upper = strings.ToUpper(statement)
		if !strings.HasSuffix(upper, "COMMIT;") {
			return "", errors.New("migration transaction has no closing COMMIT")
		}
		statement = strings.TrimSpace(statement[:len(statement)-len("COMMIT;")])
	}
	upper = strings.ToUpper(statement)
	if strings.Contains(upper, "BEGIN;") || strings.Contains(upper, "COMMIT;") {
		return "", errors.New("migration has nested transaction control")
	}
	return statement, nil
}

func safeMigrationPrelude(statement string) bool {
	upper := strings.ToUpper(strings.TrimSpace(statement))
	return strings.Contains(upper, "IF NOT EXISTS") && !strings.Contains(upper, "BEGIN;") && !strings.Contains(upper, "COMMIT;")
}

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
	connection, err := s.pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("acquire migration connection: %w", err)
	}
	defer connection.Release()
	if _, err = connection.Exec(ctx, "SELECT pg_advisory_lock($1)", migrationLockID); err != nil {
		return fmt.Errorf("lock hosted schema migrations: %w", err)
	}
	defer func() {
		unlockContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, _ = connection.Exec(unlockContext, "SELECT pg_advisory_unlock($1)", migrationLockID)
	}()
	if _, err = connection.Exec(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
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
	var databaseVersion int
	if err := connection.QueryRow(ctx, "SELECT COALESCE(max(version),0) FROM schema_migrations").Scan(&databaseVersion); err != nil {
		return fmt.Errorf("read current schema version: %w", err)
	}
	if len(versions) == 0 || databaseVersion > versions[len(versions)-1] {
		return fmt.Errorf("database schema version %d is newer than this control plane", databaseVersion)
	}
	for _, version := range versions {
		var applied bool
		if err := connection.QueryRow(ctx, "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)", version).Scan(&applied); err != nil {
			return fmt.Errorf("read migration ledger: %w", err)
		}
		if applied {
			continue
		}
		statements := migrationStatements(byVersion[version])
		for _, statement := range statements[:len(statements)-1] {
			if !safeMigrationPrelude(statement) {
				return fmt.Errorf("migration %d has a non-idempotent statement before its transaction", version)
			}
			if _, err := connection.Exec(ctx, statement); err != nil {
				return fmt.Errorf("apply hosted schema migration %d: %w", version, err)
			}
		}
		body, err := migrationTransactionBody(statements[len(statements)-1])
		if err != nil {
			return fmt.Errorf("prepare hosted schema migration %d: %w", version, err)
		}
		tx, err := connection.BeginTx(ctx, pgx.TxOptions{})
		if err != nil {
			return fmt.Errorf("begin hosted schema migration %d: %w", version, err)
		}
		if _, err = tx.Exec(ctx, body); err == nil {
			_, err = tx.Exec(ctx, "INSERT INTO schema_migrations (version) VALUES ($1)", version)
		}
		if err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("apply hosted schema migration %d: %w", version, err)
		}
		if err = tx.Commit(ctx); err != nil {
			return fmt.Errorf("commit hosted schema migration %d: %w", version, err)
		}
	}
	return nil
}

var ErrNotFound = errors.New("not found")
