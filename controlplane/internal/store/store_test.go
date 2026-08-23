package store

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The control plane carries its own copy of the up migrations because go:embed
// cannot reach outside the module, while contracts/postgres/ is the published
// contract and the only home of the down migrations. Neither copy can be
// removed, so this is what keeps them from drifting apart unnoticed.
func TestEmbeddedMigrationsMatchThePublishedContract(t *testing.T) {
	const contractDir = "../../../contracts/postgres"
	published, err := os.ReadDir(contractDir)
	if err != nil {
		t.Fatal(err)
	}
	unmatched := make(map[string]bool)
	for _, entry := range published {
		if strings.HasSuffix(entry.Name(), ".up.sql") {
			unmatched[entry.Name()] = true
		}
	}
	embedded, err := migrationFiles.ReadDir("migrations")
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range embedded {
		name := entry.Name()
		if !unmatched[name] {
			t.Errorf("%s is embedded but not published under %s", name, contractDir)
			continue
		}
		delete(unmatched, name)
		deployed, err := migrationFiles.ReadFile("migrations/" + name)
		if err != nil {
			t.Fatal(err)
		}
		contract, err := os.ReadFile(filepath.Join(contractDir, name))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(deployed, contract) {
			t.Errorf("%s differs from the migration published as the contract", name)
		}
	}
	for name := range unmatched {
		t.Errorf("%s is published as the contract but not embedded", name)
	}
}

// A new enum value cannot be used in the same transaction that adds it, so
// these migrations only work if the break survives into the deployed copy.
// 0009 depends on it twice over: its CHECK constraint names the 'localize'
// operation that its own first statement introduces.
func TestDeployedMigrationsRetainStatementBoundaries(t *testing.T) {
	for name, want := range map[string]int{
		"migrations/0002_internal_validation.up.sql":       2,
		"migrations/0004_analysis_result_artifacts.up.sql": 2,
		"migrations/0009_localize_operation.up.sql":        4,
	} {
		contents, err := migrationFiles.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		if statements := migrationStatements(string(contents)); len(statements) != want {
			t.Fatalf("%s split into %d statements, want %d", name, len(statements), want)
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
