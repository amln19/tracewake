package controlplane

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/jackc/pgx/v5"
)

const BrowserSessionLifetime = 15 * time.Minute

type BrowserSession struct {
	Token     string
	CSRFToken string
	ExpiresAt time.Time
	Scopes    []string
}

func (s *Service) ExchangeBrowserSession(ctx context.Context, token string) (BrowserSession, error) {
	principal, err := s.authenticateToken(ctx, token, "")
	if err != nil {
		return BrowserSession{}, err
	}
	id, err := newID()
	if err != nil {
		return BrowserSession{}, err
	}
	prefix, err := randomPrefix("session")
	if err != nil {
		return BrowserSession{}, err
	}
	sessionToken, verifier, err := s.tokens.NewToken(prefix)
	if err != nil {
		return BrowserSession{}, err
	}
	csrfToken, csrfVerifier, err := s.tokens.NewToken("csrf")
	if err != nil {
		return BrowserSession{}, err
	}
	expires := time.Now().UTC().Add(BrowserSessionLifetime)
	scopes := scopeNames(principal.Scopes)
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return BrowserSession{}, fmt.Errorf("begin browser session: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, `INSERT INTO browser_sessions
        (id,workspace_id,prefix,verifier,csrf_verifier,pepper_version,scopes,expires_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, id, principal.WorkspaceID, prefix,
		verifier, csrfVerifier, s.tokens.CurrentVersion, scopes, expires); err != nil {
		return BrowserSession{}, fmt.Errorf("insert browser session: %w", err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO audit_records
        (workspace_id,aggregate_type,aggregate_id,event_type,actor_type)
        VALUES($1,'browser_session',$2,'browser_session.created','tenant')`, principal.WorkspaceID, id); err != nil {
		return BrowserSession{}, fmt.Errorf("audit browser session: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return BrowserSession{}, fmt.Errorf("commit browser session: %w", err)
	}
	return BrowserSession{Token: sessionToken, CSRFToken: csrfToken, ExpiresAt: expires, Scopes: scopes}, nil
}

func (s *Service) RefreshBrowserSession(ctx context.Context, token string) (BrowserSession, error) {
	principal, id, expires, scopes, err := s.authenticateBrowserSession(ctx, token, "", "", false)
	if err != nil {
		return BrowserSession{}, err
	}
	csrfToken, csrfVerifier, err := s.tokens.NewToken("csrf")
	if err != nil {
		return BrowserSession{}, err
	}
	command, err := s.pool.Exec(ctx, `UPDATE browser_sessions
	        SET verifier=$1,csrf_verifier=$2,pepper_version=$3,last_used_at=transaction_timestamp()
	        WHERE id=$4 AND workspace_id=$5 AND revoked_at IS NULL AND expires_at>transaction_timestamp()`,
		hmacDigest(s.tokens.Current, token), csrfVerifier, s.tokens.CurrentVersion, id, principal.WorkspaceID)
	if err != nil {
		return BrowserSession{}, fmt.Errorf("refresh browser session: %w", err)
	}
	if command.RowsAffected() != 1 {
		return BrowserSession{}, ErrUnauthenticated
	}
	return BrowserSession{CSRFToken: csrfToken, ExpiresAt: expires, Scopes: scopes}, nil
}

func (s *Service) AuthenticateBrowserSession(ctx context.Context, token, requiredScope, csrfToken string, requireCSRF bool) (Principal, string, error) {
	principal, id, _, _, err := s.authenticateBrowserSession(ctx, token, requiredScope, csrfToken, requireCSRF)
	return principal, id, err
}

func (s *Service) authenticateBrowserSession(ctx context.Context, token, requiredScope, csrfToken string, requireCSRF bool) (Principal, string, time.Time, []string, error) {
	prefix, err := splitToken(token)
	if err != nil {
		return Principal{}, "", time.Time{}, nil, ErrUnauthenticated
	}
	var id, workspaceID string
	var verifier, csrfVerifier []byte
	var pepperVersion int16
	var scopes []string
	var expires time.Time
	err = s.pool.QueryRow(ctx, `SELECT b.id,b.workspace_id,b.verifier,b.csrf_verifier,b.pepper_version,b.scopes,b.expires_at
        FROM browser_sessions b JOIN workspaces w ON w.id=b.workspace_id
        WHERE b.prefix=$1 AND b.revoked_at IS NULL AND b.expires_at>transaction_timestamp()
          AND w.state='active'`, prefix).Scan(&id, &workspaceID, &verifier, &csrfVerifier, &pepperVersion, &scopes, &expires)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Principal{}, "", time.Time{}, nil, ErrUnauthenticated
		}
		return Principal{}, "", time.Time{}, nil, fmt.Errorf("read browser session: %w", err)
	}
	if !s.tokens.Verify(pepperVersion, token, verifier) {
		return Principal{}, "", time.Time{}, nil, ErrUnauthenticated
	}
	if requireCSRF && (csrfToken == "" || !s.tokens.Verify(pepperVersion, csrfToken, csrfVerifier)) {
		return Principal{}, "", time.Time{}, nil, ErrForbidden
	}
	principal := Principal{WorkspaceID: workspaceID, Scopes: make(map[string]bool, len(scopes))}
	for _, scope := range scopes {
		principal.Scopes[scope] = true
	}
	if requiredScope != "" && !principal.Scopes[requiredScope] {
		return Principal{}, "", time.Time{}, nil, ErrForbidden
	}
	if _, err := s.pool.Exec(ctx, "UPDATE browser_sessions SET last_used_at=transaction_timestamp() WHERE id=$1", id); err != nil {
		return Principal{}, "", time.Time{}, nil, fmt.Errorf("record browser session use: %w", err)
	}
	return principal, id, expires, scopes, nil
}

func (s *Service) RevokeBrowserSession(ctx context.Context, token, csrfToken string) error {
	principal, id, _, _, err := s.authenticateBrowserSession(ctx, token, "", csrfToken, true)
	if err != nil {
		return err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin browser session revocation: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, "UPDATE browser_sessions SET revoked_at=transaction_timestamp() WHERE id=$1 AND revoked_at IS NULL", id); err != nil {
		return fmt.Errorf("revoke browser session: %w", err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO audit_records
        (workspace_id,aggregate_type,aggregate_id,event_type,actor_type)
        VALUES($1,'browser_session',$2,'browser_session.revoked','tenant')`, principal.WorkspaceID, id); err != nil {
		return fmt.Errorf("audit browser session revocation: %w", err)
	}
	return tx.Commit(ctx)
}

func scopeNames(scopes map[string]bool) []string {
	result := make([]string, 0, len(scopes))
	for scope, enabled := range scopes {
		if enabled {
			result = append(result, scope)
		}
	}
	sort.Strings(result)
	return result
}
