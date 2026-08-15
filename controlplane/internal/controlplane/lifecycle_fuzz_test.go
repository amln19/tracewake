package controlplane

import "testing"

func FuzzNormalizedDigest(f *testing.F) {
	f.Add("diff", "a", "b", "align-v1")
	f.Add("otlp", "a", "", "")
	f.Fuzz(func(t *testing.T, operation, a, b, profile string) {
		runs := []string{a}
		if b != "" {
			runs = append(runs, b)
		}
		var selected *string
		if profile != "" {
			selected = &profile
		}
		_, _ = normalizedDigest(JobRequest{Operation: operation, RunIDs: runs, Profile: selected})
	})
}
