package protocol

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
)

const EngineFinancialProvider = "292a67aebe534769d9be7c9fea72bd94fc20c8f6"

func validateEngineFinancialConfig(c map[string]any) error {
	if exact(c, "provider_revision", "checkpoint_format", "policy", "provider_binding_schema") != nil || c["provider_revision"] != EngineFinancialProvider || c["checkpoint_format"] != "engine-financial-checkpoint.v1" || c["policy"] != "target-weights.v1" || c["provider_binding_schema"] != "runtime-engine-binding.v1" {
		return fail("unsupported Engine provider/format/policy/binding")
	}
	return nil
}

func ValidateEngineCheckpoint(m map[string]any) error {
	if exact(m, "schema", "job_id", "attempt", "token", "binding", "cursor", "checkpoint_base64", "checkpoint_sha256") != nil || m["attempt"] != m["token"] {
		return fail("invalid Engine checkpoint fields/attempt")
	}
	encoded, ok := m["checkpoint_base64"].(string)
	if !ok {
		return fail("missing Engine checkpoint bytes")
	}
	raw, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil {
		return fail("invalid Engine checkpoint base64")
	}
	sum := sha256.Sum256(raw)
	inner, err := DecodeObject(raw)
	if err != nil || hex.EncodeToString(sum[:]) != m["checkpoint_sha256"] || inner["schema"] != "engine-financial-checkpoint.v1" || inner["cursor"] != m["cursor"] {
		return fail("Engine checkpoint identity mismatch")
	}
	return nil
}
