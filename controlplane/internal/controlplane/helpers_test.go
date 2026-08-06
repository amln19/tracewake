package controlplane_test

import (
	"context"
	"os"
	"testing"

	"github.com/amln19/locus/controlplane/internal/controlplane"
	"github.com/amln19/locus/controlplane/internal/store"
	"github.com/jackc/pgx/v5/pgxpool"
)

// newTestService opens the shared test database and returns a service with its
// own workspace, so hosted tests never observe each other's rows.
func newTestService(t *testing.T) (*controlplane.Service, *pgxpool.Pool, string) {
	t.Helper()
	databaseURL := os.Getenv("LOCUS_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("LOCUS_TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	database, err := store.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(database.Close)
	if err := database.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	ring := controlplane.KeyRing{CurrentVersion: 1, Current: []byte("integration-test-pepper-material")}
	service, err := controlplane.New(database.Pool(), ring, ring)
	if err != nil {
		t.Fatal(err)
	}
	workspace, _, err := service.CreateWorkspace(ctx, "hosted-test", []string{"runs:read", "runs:write", "jobs:read", "jobs:write", "artifacts:read", "audit:read"})
	if err != nil {
		t.Fatal(err)
	}
	return service, database.Pool(), workspace
}

func readyRun(t *testing.T, pool *pgxpool.Pool, workspace string) string {
	t.Helper()
	run := testID(t)
	_, err := pool.Exec(context.Background(), `INSERT INTO runs(id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key,bundle_object_version,validated_bundle_format,cassette_format_version,event_schema_version,logical_run_digest,event_count,ready_at)
        VALUES($1,$2,'ready',1,$3,1,$4,$5,1,1,3,$6,1,transaction_timestamp())`,
		run, workspace, digest("bundle-"+run), "workspaces/"+workspace+"/runs/"+run+"/bundle.tar", digest("version-"+run), digest("logical-"+run))
	if err != nil {
		t.Fatal(err)
	}
	return run
}
