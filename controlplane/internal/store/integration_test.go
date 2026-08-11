package store_test

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/amln19/tracewake/controlplane/internal/store"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestMigrateFromSchemaOne(t *testing.T) {
	databaseURL := os.Getenv("TRACEWAKE_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("TRACEWAKE_TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	admin, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	var random [8]byte
	if _, err := rand.Read(random[:]); err != nil {
		t.Fatal(err)
	}
	schema := "migration_" + hex.EncodeToString(random[:])
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatal(err)
	}
	defer func() { _, _ = admin.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE") }()
	schemaURL := databaseURL
	separator := "?"
	if strings.Contains(schemaURL, "?") {
		separator = "&"
	}
	schemaURL += separator + "search_path=" + schema
	legacy, err := store.Open(ctx, schemaURL)
	if err != nil {
		t.Fatal(err)
	}
	defer legacy.Close()
	migration, err := os.ReadFile("../../../contracts/postgres/0001_hosted_contracts.up.sql")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.Pool().Exec(ctx, string(migration)); err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.Pool().Exec(ctx, "CREATE TABLE schema_migrations(version integer PRIMARY KEY,applied_at timestamptz NOT NULL DEFAULT transaction_timestamp()); INSERT INTO schema_migrations(version) VALUES(1)"); err != nil {
		t.Fatal(err)
	}
	if err := legacy.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	var versions []int
	if err := legacy.Pool().QueryRow(ctx, "SELECT array_agg(version ORDER BY version) FROM schema_migrations").Scan(&versions); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(versions, []int{1, 2, 3, 4, 5, 6, 7}) {
		t.Fatalf("versions=%v", versions)
	}
	var activeDigestIndex string
	if err := legacy.Pool().QueryRow(ctx, `SELECT indexdef FROM pg_indexes
		WHERE schemaname=current_schema() AND indexname='runs_workspace_active_digest_idx'`).Scan(&activeDigestIndex); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(activeDigestIndex, "WHERE (state <> 'deleted'::ingestion_state)") {
		t.Fatalf("active digest index=%q", activeDigestIndex)
	}
	// A database created before the widening holds varchar(24) prefixes, which
	// cannot store a tenant token. The forward migration has to reach it.
	var prefixWidths []int
	if err := legacy.Pool().QueryRow(ctx, `SELECT array_agg(character_maximum_length ORDER BY table_name)
		FROM information_schema.columns
		WHERE table_schema=current_schema() AND column_name='prefix'
		AND table_name IN ('api_tokens','browser_sessions','worker_credentials')`).Scan(&prefixWidths); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(prefixWidths, []int{26, 26, 26}) {
		t.Fatalf("token prefix widths=%v", prefixWidths)
	}
	var sessionColumns int
	if err := legacy.Pool().QueryRow(ctx, `SELECT count(*) FROM information_schema.columns
		WHERE table_schema=current_schema() AND table_name='browser_sessions'
		AND column_name IN ('verifier','csrf_verifier','expires_at','revoked_at')`).Scan(&sessionColumns); err != nil {
		t.Fatal(err)
	}
	if sessionColumns != 4 {
		t.Fatalf("browser session columns=%d", sessionColumns)
	}
	var labels []string
	if err := legacy.Pool().QueryRow(ctx, "SELECT enum_range(NULL::job_operation)::text[]").Scan(&labels); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(labels, "validate") {
		t.Fatalf("job operations=%v", labels)
	}
	var kinds []string
	if err := legacy.Pool().QueryRow(ctx, "SELECT enum_range(NULL::artifact_kind)::text[]").Scan(&kinds); err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"validation_json", "otlp_result_json", "pprof_result_json"} {
		if !slices.Contains(kinds, kind) {
			t.Fatalf("artifact kinds=%v", kinds)
		}
	}
	if _, err := legacy.Pool().Exec(ctx, "INSERT INTO schema_migrations(version) VALUES(99)"); err != nil {
		t.Fatal(err)
	}
	if err := legacy.Migrate(ctx); err == nil {
		t.Fatal("future schema version was accepted")
	}
}
