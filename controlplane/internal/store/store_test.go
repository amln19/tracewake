package store

import (
	"strings"
	"testing"
)

func TestDeployedMigrationsRetainStatementBoundaries(t *testing.T) {
	for _, name := range []string{
		"migrations/0002_internal_validation.up.sql",
		"migrations/0004_analysis_result_artifacts.up.sql",
	} {
		contents, err := migrationFiles.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		if statements := migrationStatements(string(contents)); len(statements) != 2 {
			t.Fatalf("%s split into %d statements, want 2", name, len(statements))
		}
	}
}

func TestMigrationTransactionBodyKeepsDDLWithItsLedgerEntry(t *testing.T) {
	body, err := migrationTransactionBody("BEGIN;\nCREATE TABLE example(id integer);\nCOMMIT;")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(body, "BEGIN") || strings.Contains(body, "COMMIT") || !strings.Contains(body, "CREATE TABLE") {
		t.Fatalf("body=%q", body)
	}
	if safeMigrationPrelude("ALTER TABLE example ADD COLUMN value text") {
		t.Fatal("a non-idempotent migration prelude was accepted")
	}
}
