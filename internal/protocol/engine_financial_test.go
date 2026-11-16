package protocol

import (
	"strings"
	"testing"
)

const engineFinancialRequest = `{"schema_version":9,"idempotency_key":"synthetic-engine","backend":"engine.minute_session_resume","evidence_tier":"diagnostic","inputs":{"product_manifest_ref":"artifact://sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"config":{"provider_revision":"ad0895f1c2015eb31bd364b40741b0f7cd6e0048","checkpoint_format":"engine-financial-checkpoint.v1","policy":"target-weights.v1","provider_binding_schema":"runtime-engine-binding.v1"},"execution":{},"budgets":{"wall_seconds":30,"memory_mb":4096}}`

func TestEngineFinancialRequest(t *testing.T) {
	r, err := Decode([]byte(engineFinancialRequest))
	if err != nil {
		t.Fatal(err)
	}
	for _, old := range []string{"platform.minute_session_resume", "evil.module"} {
		if _, err = Decode([]byte(strings.Replace(engineFinancialRequest, "engine.minute_session_resume", old, 1))); err == nil {
			t.Fatal("accepted cross-provider", old)
		}
	}
	again, err := Decode([]byte(r.CanonicalJSON))
	if err != nil || again.SHA256 != r.SHA256 {
		t.Fatal("changed identity", err)
	}
	for _, field := range []string{"provider_revision", "provider_binding_schema", "checkpoint_format", "policy"} {
		altered := strings.Replace(engineFinancialRequest, `"`+field+`":`, `"unknown":`, 1)
		if _, err = Decode([]byte(altered)); err == nil {
			t.Fatal("accepted unknown config", field)
		}
	}
}

func TestEngineRejectsLegacyCheckpointBytes(t *testing.T) {
	for _, inner := range []string{"minute-session.v2", "minute-session.v3", "other-producer.v1"} {
		if ValidateEngineCheckpoint(map[string]any{"checkpoint": map[string]any{"schema": inner}}) == nil {
			t.Fatal("accepted incompatible checkpoint", inner)
		}
	}
}

func TestV8FinancialFingerprintUnchanged(t *testing.T) {
	r, err := Decode([]byte(`{"backend":"platform.minute_session_resume","budgets":{"memory_mb":4096,"wall_seconds":30},"config":{"checkpoint_format":"minute-session.v2","policy":"target-weights.v1","provider_revision":"7a618bddd8fdeee5dbe37fe5b54a9698e0de5355"},"evidence_tier":"diagnostic","execution":{},"idempotency_key":"synthetic-minute","inputs":{"minute_input_ref":"artifact://sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"schema_version":8}`))
	if err != nil || r.SHA256 != "bc90da0ecc83a53e33cdce176eef746635fc218ee2186c792dbe1e7d8f1f3b79" {
		t.Fatal("v8 fingerprint changed", r, err)
	}
}
