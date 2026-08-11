package controlplane_test

import (
	"context"
	"errors"
	"os"
	"testing"

	"github.com/amln19/tracewake/controlplane/internal/controlplane"
	"github.com/amln19/tracewake/controlplane/internal/store"
)

func TestBootstrapTokenAndPepperRotation(t *testing.T) {
	databaseURL := os.Getenv("TRACEWAKE_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("TRACEWAKE_TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	database, err := store.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(database.Close)
	if err = database.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	oldPepper := []byte("old-bootstrap-pepper-material-000000")
	currentPepper := []byte("new-bootstrap-pepper-material-000000")
	oldService, err := controlplane.New(database.Pool(), controlplane.KeyRing{CurrentVersion: 1, Current: oldPepper}, controlplane.KeyRing{CurrentVersion: 1, Current: oldPepper})
	if err != nil {
		t.Fatal(err)
	}
	prefix := "tracewake_" + testID(t)[:16]
	oldToken := prefix + ".abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
	workspace, err := oldService.EnsureWorkspaceToken(ctx, "bootstrap-rotation", oldToken, []string{"runs:read"})
	if err != nil {
		t.Fatal(err)
	}

	rotatedService, err := controlplane.New(database.Pool(), controlplane.KeyRing{CurrentVersion: 2, Current: currentPepper, Previous: oldPepper}, controlplane.KeyRing{CurrentVersion: 2, Current: currentPepper, Previous: oldPepper})
	if err != nil {
		t.Fatal(err)
	}
	principal, err := rotatedService.Authenticate(ctx, oldToken, "runs:read")
	if err != nil || principal.WorkspaceID != workspace {
		t.Fatalf("previous pepper did not authenticate: principal=%+v error=%v", principal, err)
	}
	var version int
	if err = database.Pool().QueryRow(ctx, "SELECT pepper_version FROM api_tokens WHERE prefix=$1", prefix).Scan(&version); err != nil || version != 2 {
		t.Fatalf("token verifier was not migrated: version=%d error=%v", version, err)
	}

	newToken := prefix + ".ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210abcdefg"
	gotWorkspace, err := rotatedService.EnsureWorkspaceToken(ctx, "bootstrap-rotation", newToken, []string{"jobs:read"})
	if err != nil || gotWorkspace != workspace {
		t.Fatalf("same-prefix bootstrap replacement workspace=%s error=%v", gotWorkspace, err)
	}
	if _, err = rotatedService.Authenticate(ctx, oldToken, ""); !errors.Is(err, controlplane.ErrUnauthenticated) {
		t.Fatalf("old bootstrap secret still authenticated: %v", err)
	}
	if _, err = rotatedService.Authenticate(ctx, newToken, "jobs:read"); err != nil {
		t.Fatalf("replacement bootstrap secret did not authenticate: %v", err)
	}
}
