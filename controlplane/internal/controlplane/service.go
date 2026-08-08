package controlplane

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"

	"github.com/amln19/tracewake/controlplane/internal/telemetry"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Service struct {
	pool    *pgxpool.Pool
	tokens  KeyRing
	workers KeyRing
	metrics *telemetry.Metrics
}

type Principal struct {
	WorkspaceID string
	Scopes      map[string]bool
}

func New(pool *pgxpool.Pool, tokens, workers KeyRing) (*Service, error) {
	if len(tokens.Current) == 0 || tokens.CurrentVersion < 1 || len(workers.Current) == 0 || workers.CurrentVersion < 1 {
		return nil, errors.New("current token and worker peppers are required")
	}
	return &Service{pool: pool, tokens: tokens, workers: workers, metrics: telemetry.NoMetrics()}, nil
}

// UseTelemetry replaces the recorder lifecycle transitions report to. A service
// that is never given one records nothing.
func (s *Service) UseTelemetry(metrics *telemetry.Metrics) { s.metrics = metrics }

func (s *Service) CreateWorkspace(ctx context.Context, name string, scopes []string) (string, string, error) {
	if strings.TrimSpace(name) == "" || len(name) > 200 {
		return "", "", errors.New("workspace name must contain 1 to 200 characters")
	}
	if len(scopes) == 0 || len(scopes) > 16 {
		return "", "", errors.New("workspace token must have 1 to 16 scopes")
	}
	workspaceID, err := newID()
	if err != nil {
		return "", "", err
	}
	tokenID, err := newID()
	if err != nil {
		return "", "", err
	}
	prefix, err := randomPrefix("tracewake")
	if err != nil {
		return "", "", err
	}
	token, verifier, err := s.tokens.NewToken(prefix)
	if err != nil {
		return "", "", err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", "", fmt.Errorf("begin workspace transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, "INSERT INTO workspaces (id, name) VALUES ($1, $2)", workspaceID, name); err != nil {
		return "", "", fmt.Errorf("insert workspace: %w", err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO api_tokens
        (id, workspace_id, prefix, verifier, pepper_version, scopes)
        VALUES ($1, $2, $3, $4, $5, $6)`, tokenID, workspaceID, prefix, verifier, s.tokens.CurrentVersion, scopes); err != nil {
		return "", "", fmt.Errorf("insert workspace token: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return "", "", fmt.Errorf("commit workspace transaction: %w", err)
	}
	return workspaceID, token, nil
}

func (s *Service) CreateWorkerCredential(ctx context.Context) (string, string, error) {
	id, err := newID()
	if err != nil {
		return "", "", err
	}
	prefix, err := randomPrefix("worker")
	if err != nil {
		return "", "", err
	}
	token, verifier, err := s.workers.NewToken(prefix)
	if err != nil {
		return "", "", err
	}
	_, err = s.pool.Exec(ctx, `INSERT INTO worker_credentials(id,prefix,verifier,pepper_version) VALUES($1,$2,$3,$4)`, id, prefix, verifier, s.workers.CurrentVersion)
	if err != nil {
		return "", "", fmt.Errorf("insert worker credential: %w", err)
	}
	return id, token, nil
}

// EnsureWorkspaceToken registers a token the deployment already holds in its
// secret store. Hosted deployments have nowhere safe to print a generated
// token, so the operator supplies it and the control plane stores only its
// verifier.
func (s *Service) EnsureWorkspaceToken(ctx context.Context, name, token string, scopes []string) (string, error) {
	prefix, err := splitToken(token)
	if err != nil {
		return "", errors.New("bootstrap token must be a prefixed high-entropy secret")
	}
	var workspaceID string
	if err := s.pool.QueryRow(ctx, "SELECT workspace_id FROM api_tokens WHERE prefix=$1", prefix).Scan(&workspaceID); err == nil {
		return workspaceID, nil
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return "", fmt.Errorf("read bootstrap token: %w", err)
	}
	workspaceID, err = newID()
	if err != nil {
		return "", err
	}
	tokenID, err := newID()
	if err != nil {
		return "", err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", fmt.Errorf("begin bootstrap transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, "INSERT INTO workspaces (id, name) VALUES ($1, $2)", workspaceID, name); err != nil {
		return "", fmt.Errorf("insert bootstrap workspace: %w", err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO api_tokens (id, workspace_id, prefix, verifier, pepper_version, scopes)
        VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (prefix) DO NOTHING`, tokenID, workspaceID, prefix, hmacDigest(s.tokens.Current, token), s.tokens.CurrentVersion, scopes); err != nil {
		return "", fmt.Errorf("insert bootstrap token: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return "", fmt.Errorf("commit bootstrap transaction: %w", err)
	}
	if err := s.pool.QueryRow(ctx, "SELECT workspace_id FROM api_tokens WHERE prefix=$1", prefix).Scan(&workspaceID); err != nil {
		return "", fmt.Errorf("read bootstrap token: %w", err)
	}
	return workspaceID, nil
}

// EnsureWorkerCredential registers the worker secret both services receive
// from the deployment's secret store.
func (s *Service) EnsureWorkerCredential(ctx context.Context, token string) (string, error) {
	prefix, err := splitToken(token)
	if err != nil {
		return "", errors.New("worker credential must be a prefixed high-entropy secret")
	}
	id, err := newID()
	if err != nil {
		return "", err
	}
	if _, err := s.pool.Exec(ctx, `INSERT INTO worker_credentials(id,prefix,verifier,pepper_version)
        VALUES($1,$2,$3,$4) ON CONFLICT (prefix) DO UPDATE SET verifier=EXCLUDED.verifier,pepper_version=EXCLUDED.pepper_version,revoked_at=NULL`,
		id, prefix, hmacDigest(s.workers.Current, token), s.workers.CurrentVersion); err != nil {
		return "", fmt.Errorf("register worker credential: %w", err)
	}
	if err := s.pool.QueryRow(ctx, "SELECT id FROM worker_credentials WHERE prefix=$1", prefix).Scan(&id); err != nil {
		return "", fmt.Errorf("read worker credential: %w", err)
	}
	return id, nil
}

func (s *Service) AuthenticateWorker(ctx context.Context, token string) (string, error) {
	prefix, err := splitToken(token)
	if err != nil {
		return "", ErrUnauthenticated
	}
	var id string
	var verifier []byte
	var version int16
	err = s.pool.QueryRow(ctx, `SELECT id,verifier,pepper_version FROM worker_credentials WHERE prefix=$1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>transaction_timestamp())`, prefix).Scan(&id, &verifier, &version)
	if err != nil || !s.workers.Verify(version, token, verifier) {
		return "", ErrUnauthenticated
	}
	return id, nil
}

func (s *Service) Authenticate(ctx context.Context, token string, requiredScope string) (Principal, error) {
	return s.authenticateToken(ctx, token, requiredScope)
}

func (s *Service) authenticateToken(ctx context.Context, token string, requiredScope string) (Principal, error) {
	prefix, err := splitToken(token)
	if err != nil {
		return Principal{}, ErrUnauthenticated
	}
	var workspaceID string
	var verifier []byte
	var pepperVersion int16
	var scopes []string
	err = s.pool.QueryRow(ctx, `SELECT workspace_id, verifier, pepper_version, scopes
        FROM api_tokens JOIN workspaces ON workspaces.id = api_tokens.workspace_id
        WHERE prefix = $1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > transaction_timestamp())
          AND workspaces.state = 'active'`, prefix).Scan(&workspaceID, &verifier, &pepperVersion, &scopes)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Principal{}, ErrUnauthenticated
		}
		return Principal{}, fmt.Errorf("read API token: %w", err)
	}
	if !s.tokens.Verify(pepperVersion, token, verifier) {
		return Principal{}, ErrUnauthenticated
	}
	principal := Principal{WorkspaceID: workspaceID, Scopes: make(map[string]bool, len(scopes))}
	for _, scope := range scopes {
		principal.Scopes[scope] = true
	}
	if requiredScope != "" && !principal.Scopes[requiredScope] {
		return Principal{}, ErrForbidden
	}
	if _, err := s.pool.Exec(ctx, "UPDATE api_tokens SET last_used_at = transaction_timestamp() WHERE prefix = $1", prefix); err != nil {
		return Principal{}, fmt.Errorf("record API token use: %w", err)
	}
	return principal, nil
}

func randomPrefix(kind string) (string, error) {
	var bytes [8]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		return "", fmt.Errorf("generate token prefix: %w", err)
	}
	return kind + "_" + hex.EncodeToString(bytes[:]), nil
}
