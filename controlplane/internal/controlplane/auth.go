package controlplane

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
)

var ErrUnauthenticated = errors.New("unauthenticated")

type KeyRing struct {
	CurrentVersion int16
	Current        []byte
	Previous       []byte
}

func (k KeyRing) Verify(version int16, token string, verifier []byte) bool {
	key := k.Current
	if version != k.CurrentVersion {
		if version != k.CurrentVersion-1 || len(k.Previous) == 0 {
			return false
		}
		key = k.Previous
	}
	actual := hmacDigest(key, token)
	return hmac.Equal(actual, verifier)
}

func (k KeyRing) NewToken(prefix string) (string, []byte, error) {
	if len(k.Current) == 0 || k.CurrentVersion < 1 {
		return "", nil, errors.New("current token pepper is required")
	}
	var secret [32]byte
	if _, err := rand.Read(secret[:]); err != nil {
		return "", nil, fmt.Errorf("generate token secret: %w", err)
	}
	token := prefix + "." + base64.RawURLEncoding.EncodeToString(secret[:])
	return token, hmacDigest(k.Current, token), nil
}

func hmacDigest(key []byte, value string) []byte {
	h := hmac.New(sha256.New, key)
	_, _ = h.Write([]byte(value))
	return h.Sum(nil)
}

func splitToken(token string) (string, error) {
	prefix, secret, ok := strings.Cut(token, ".")
	if !ok || len(prefix) == 0 || len(prefix) > 24 || len(secret) < 43 {
		return "", ErrUnauthenticated
	}
	return prefix, nil
}
