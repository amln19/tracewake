package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/amln19/locus/controlplane/internal/artifacts"
	"github.com/amln19/locus/controlplane/internal/controlplane"
	"github.com/amln19/locus/controlplane/internal/httpapi"
	"github.com/amln19/locus/controlplane/internal/notify"
	"github.com/amln19/locus/controlplane/internal/store"
	"github.com/amln19/locus/controlplane/internal/workerapi"
	"github.com/aws/aws-sdk-go-v2/config"
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
	localStore, err := artifacts.NewFilesystem(envOr("LOCUS_ARTIFACT_ROOT", ".locus-hosted/artifacts"), objectSigningKey())
	if err != nil {
		log.Fatal(err)
	}
	var artifactStore artifacts.Store = localStore
	if bucket := strings.TrimSpace(os.Getenv("LOCUS_ARTIFACT_BUCKET")); bucket != "" {
		awsConfig, err := config.LoadDefaultConfig(ctx)
		if err != nil {
			log.Fatalf("load AWS configuration: %v", err)
		}
		hosted, err := artifacts.NewS3(awsConfig, bucket)
		if err != nil {
			log.Fatal(err)
		}
		artifactStore = hosted
	}
	scopes := []string{"runs:read", "runs:write", "jobs:read", "jobs:write", "artifacts:read", "audit:read"}
	if len(os.Args) > 1 && os.Args[1] == "bootstrap" {
		workspace, token, err := service.CreateWorkspace(ctx, "local", scopes)
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
			workspace, token, err := service.CreateWorkspace(ctx, "local", scopes)
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
	if token := strings.TrimSpace(os.Getenv("LOCUS_BOOTSTRAP_TOKEN")); token != "" {
		if _, err := service.EnsureWorkspaceToken(ctx, envOr("LOCUS_BOOTSTRAP_WORKSPACE", "default"), token, scopes); err != nil {
			log.Fatal(err)
		}
	}
	if token := strings.TrimSpace(os.Getenv("LOCUS_WORKER_BOOTSTRAP_TOKEN")); token != "" {
		if _, err := service.EnsureWorkerCredential(ctx, token); err != nil {
			log.Fatal(err)
		}
	}
	var publisher controlplane.Notifier
	if queueURL := strings.TrimSpace(os.Getenv("LOCUS_JOB_QUEUE_URL")); queueURL != "" {
		awsConfig, err := config.LoadDefaultConfig(ctx)
		if err != nil {
			log.Fatalf("load AWS configuration: %v", err)
		}
		if publisher, err = notify.NewSQS(awsConfig, queueURL); err != nil {
			log.Fatal(err)
		}
	}
	publicAddr := envOr("LOCUS_LISTEN_ADDR", "127.0.0.1:8080")
	workerAddr := envOr("LOCUS_WORKER_LISTEN_ADDR", "127.0.0.1:8081")
	publicBase := envOr("LOCUS_PUBLIC_BASE_URL", reachableURL(publicAddr))
	workerBase := envOr("LOCUS_WORKER_BASE_URL", reachableURL(workerAddr))
	publicMux := http.NewServeMux()
	publicMux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	publicMux.Handle("/", httpapi.New(service, artifactStore, publicBase).Handler())
	workerMux := http.NewServeMux()
	workerMux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	workerMux.Handle("/", workerapi.New(service, artifactStore, workerBase).Handler())
	if localStore == artifactStore {
		publicMux.Handle("/objects/", localStore.Handler())
		workerMux.Handle("/objects/", localStore.Handler())
	}
	publicServer := &http.Server{Addr: publicAddr, Handler: publicMux, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second}
	workerServer := &http.Server{Addr: workerAddr, Handler: workerMux, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second}
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
		var lastRetention time.Time
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				if _, err := service.Reconcile(ctx, 100); err != nil && !errors.Is(err, context.Canceled) {
					log.Printf("reconcile: %v", err)
				}
				if publisher != nil {
					if _, err := service.PublishOutbox(ctx, publisher, 100); err != nil && !errors.Is(err, context.Canceled) {
						log.Printf("publish notifications: %v", err)
					}
				}
				// Listing every stored object is cheap locally and billed in
				// object storage, so retention runs on its own slower cadence.
				if now.Sub(lastRetention) < retentionInterval {
					continue
				}
				lastRetention = now
				if keys, err := service.RetainedObjectKeys(ctx); err == nil {
					if _, err = artifactStore.Cleanup(ctx, keys, time.Now().Add(-24*time.Hour)); err != nil {
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

const retentionInterval = 10 * time.Minute

// Signed object URLs are an extension of worker and tenant authentication, so
// they are derived from the worker pepper rather than configured separately.
func objectSigningKey() []byte {
	mac := hmac.New(sha256.New, []byte(os.Getenv("LOCUS_WORKER_PEPPER")))
	_, _ = mac.Write([]byte("locus-object-url-v1"))
	return mac.Sum(nil)
}

func reachableURL(addr string) string {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return "http://" + addr
	}
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	return "http://" + net.JoinHostPort(host, port)
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
