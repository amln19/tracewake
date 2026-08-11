package main

import "testing"

func TestKeyRingReadsCurrentAndPreviousVersions(t *testing.T) {
	t.Setenv("TEST_PEPPER", "current-pepper-material-at-least-32-bytes")
	t.Setenv("TEST_PEPPER_VERSION", "2")
	t.Setenv("TEST_PEPPER_PREVIOUS", "previous-pepper-material-at-least-32-bytes")
	ring, err := keyRingFromEnvironment("TEST_PEPPER")
	if err != nil {
		t.Fatal(err)
	}
	if ring.CurrentVersion != 2 || string(ring.Previous) != "previous-pepper-material-at-least-32-bytes" {
		t.Fatalf("ring=%+v", ring)
	}
}

func TestKeyRingRejectsInvalidRotationConfiguration(t *testing.T) {
	t.Setenv("TEST_PEPPER", "current-pepper-material-at-least-32-bytes")
	t.Setenv("TEST_PEPPER_VERSION", "1")
	t.Setenv("TEST_PEPPER_PREVIOUS", "previous-pepper-material-at-least-32-bytes")
	if _, err := keyRingFromEnvironment("TEST_PEPPER"); err == nil {
		t.Fatal("a previous pepper was accepted at version 1")
	}
}
