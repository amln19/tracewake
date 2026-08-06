package controlplane

import "testing"

func TestKeyRingAcceptsCurrentAndPreviousPepper(t *testing.T) {
	current := KeyRing{CurrentVersion: 2, Current: []byte("current secret material"), Previous: []byte("previous secret material")}
	token, verifier, err := current.NewToken("tok_123")
	if err != nil {
		t.Fatal(err)
	}
	if !current.Verify(2, token, verifier) {
		t.Fatal("current verifier was rejected")
	}
	old := KeyRing{CurrentVersion: 1, Current: current.Previous}
	oldToken, oldVerifier, err := old.NewToken("tok_456")
	if err != nil {
		t.Fatal(err)
	}
	if !current.Verify(1, oldToken, oldVerifier) {
		t.Fatal("previous verifier was rejected")
	}
}

func TestSplitTokenRejectsMalformedValues(t *testing.T) {
	for _, token := range []string{"", "prefix", ".secret", "a.x"} {
		if _, err := splitToken(token); err == nil {
			t.Fatalf("accepted %q", token)
		}
	}
}
