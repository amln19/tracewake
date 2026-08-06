package controlplane

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Service struct {
	pool   *pgxpool.Pool
	tokens KeyRing
}

type Principal struct {
	WorkspaceID string
	Scopes      map[string]bool
}

func New(pool *pgxpool.Pool, tokens KeyRing) (*Service, error) {
	if len(tokens.Current) == 0 || tokens.CurrentVersion < 1 {
		return nil, errors.New("a current token pepper is required")
	}
	return &Service{pool: pool, tokens: tokens}, nil
}

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
	prefix, err := randomPrefix("locus")
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

func (s *Service) Authenticate(ctx context.Context, token string, requiredScope string) (Principal, error) {
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
	if !principal.Scopes[requiredScope] {
		return Principal{}, errors.New("forbidden")
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
