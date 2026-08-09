package store

import "testing"

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
