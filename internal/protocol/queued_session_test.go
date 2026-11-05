package protocol

import (
	"strings"
	"testing"
)

func TestQueuedSessionRequest(t *testing.T) {
	raw := []byte(`{"schema_version":7,"idempotency_key":"synthetic-stream","backend":"nautilus.partition_session_replay","evidence_tier":"diagnostic","inputs":{"engine_config_ref":"artifact://sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","partition_manifest_ref":"artifact://sha256/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"config":{"executable_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},"execution":{},"budgets":{"wall_seconds":30,"memory_mb":4096}}`)
	r, err := Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	if r.SHA256 != "aa1f0441bc0859ef3dc215ad636c1aefc7e9b9f229b5e2b7dd916acda9b6024c" {
		t.Fatal("v7 fingerprint changed", r.SHA256)
	}
	if r.SchemaVersion != 7 || r.Backend != "nautilus.partition_session_replay" {
		t.Fatal(r)
	}
	for _, invalid := range []string{
		strings.Replace(string(raw), `"execution":{}`, `"execution":{"resume":true}`, 1),
		strings.Replace(string(raw), `"schema_version":7`, `"schema_version":6`, 1),
		strings.Replace(string(raw), `"partition_manifest_ref"`, `"partition_root"`, 1),
		strings.Replace(string(raw), `"artifact://sha256/b`, `"/operator/b`, 1),
		strings.Replace(string(raw), `"config":{`, `"config":{"path":"/operator/engine",`, 1),
	} {
		if _, err := Decode([]byte(invalid)); err == nil {
			t.Fatalf("accepted invalid v7: %s", invalid)
		}
	}
	again, err := Decode([]byte(r.CanonicalJSON))
	if err != nil || again.SHA256 != r.SHA256 {
		t.Fatal("fingerprint changed", err)
	}
}
