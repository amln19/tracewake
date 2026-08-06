package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/amln19/locus/controlplane/internal/artifacts"
	"github.com/amln19/locus/controlplane/internal/controlplane"
	"github.com/amln19/locus/controlplane/internal/httpapi"
	"github.com/amln19/locus/controlplane/internal/store"
	"github.com/amln19/locus/controlplane/internal/workerapi"
)

func main() {
	databaseURL := os.Getenv("LOCUS_DATABASE_URL")
	if databaseURL == "" {
		log.Fatal("LOCUS_DATABASE_URL is required")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	database, err := store.Open(ctx, databaseURL)
	if err != nil {
		log.Fatal(err)
	}
	defer database.Close()
	if err := database.Migrate(ctx); err != nil {
		log.Fatal(err)
	}
	service, err := controlplane.New(database.Pool(), keyRing("LOCUS_TOKEN_PEPPER"), keyRing("LOCUS_WORKER_PEPPER"))
	if err != nil {
		log.Fatal(err)
	}
	artifactStore, err := artifacts.New(envOr("LOCUS_ARTIFACT_ROOT", ".locus-hosted/artifacts"))
	if err != nil {
		log.Fatal(err)
	}
	if len(os.Args) > 1 && os.Args[1] == "bootstrap" {
		workspace, token, err := service.CreateWorkspace(ctx, "local", []string{"runs:read", "runs:write", "jobs:read", "jobs:write", "artifacts:read", "audit:read"})
		if err != nil {
			log.Fatal(err)
		}
		workerID, workerToken, err := service.CreateWorkerCredential(ctx)
		if err != nil {
			log.Fatal(err)
		}
		fmt.Printf("workspace_id=%s\ntoken=%s\nworker_id=%s\nworker_token=%s\n", workspace, token, workerID, workerToken)
		return
	}
	if path := os.Getenv("LOCUS_BOOTSTRAP_FILE"); path != "" {
		if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
			workspace, token, err := service.CreateWorkspace(ctx, "local", []string{"runs:read", "runs:write", "jobs:read", "jobs:write", "artifacts:read", "audit:read"})
			if err != nil {
				log.Fatal(err)
			}
			workerID, workerToken, err := service.CreateWorkerCredential(ctx)
			if err != nil {
				log.Fatal(err)
			}
			raw, err := json.Marshal(map[string]string{"workspace_id": workspace, "token": token, "worker_id": workerID, "worker_token": workerToken})
			if err != nil {
				log.Fatal(err)
			}
			if err = os.WriteFile(path, raw, 0o600); err != nil {
				log.Fatal(err)
			}
		} else if err != nil {
			log.Fatal(err)
		}
	}
	publicMux := http.NewServeMux()
	publicMux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	publicMux.Handle("/", httpapi.New(service, artifactStore).Handler())
	publicServer := &http.Server{Addr: envOr("LOCUS_LISTEN_ADDR", "127.0.0.1:8080"), Handler: publicMux, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second}
	workerServer := &http.Server{Addr: envOr("LOCUS_WORKER_LISTEN_ADDR", "127.0.0.1:8081"), Handler: workerapi.New(service, artifactStore).Handler(), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second}
	for _, server := range []*http.Server{publicServer, workerServer} {
		server := server
		go func() {
			if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Printf("serve %s: %v", server.Addr, err)
				stop()
			}
		}()
	}
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if _, err := service.Reconcile(ctx, 100); err != nil && !errors.Is(err, context.Canceled) {
					log.Printf("reconcile: %v", err)
				}
				if keys, err := service.RetainedObjectKeys(ctx); err == nil {
					if _, err = artifactStore.Cleanup(keys, time.Now().Add(-24*time.Hour)); err != nil {
						log.Printf("artifact cleanup: %v", err)
					}
				} else if !errors.Is(err, context.Canceled) {
					log.Printf("artifact retention: %v", err)
				}
			}
		}
	}()
	<-ctx.Done()
	shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for _, server := range []*http.Server{publicServer, workerServer} {
		if err := server.Shutdown(shutdown); err != nil {
			log.Printf("shutdown %s: %v", server.Addr, err)
		}
	}
}

func keyRing(name string) controlplane.KeyRing {
	value := os.Getenv(name)
	if len(value) < 32 {
		log.Fatalf("%s must contain at least 32 bytes", name)
	}
	return controlplane.KeyRing{CurrentVersion: 1, Current: []byte(value)}
}

func envOr(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}
