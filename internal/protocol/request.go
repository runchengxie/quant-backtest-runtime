// Package protocol validates immutable requests and independent engine evidence.
package protocol

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

type Budgets struct {
	WallSeconds int `json:"wall_seconds"`
	MemoryMB    int `json:"memory_mb"`
}
type Request struct {
	SchemaVersion                         int
	IdempotencyKey, Backend, EvidenceTier string
	Inputs                                map[string]string
	Config, Execution, Mapping            map[string]any
	Budgets                               Budgets
	CanonicalJSON, SHA256                 string
}

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
var keyPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)

func fail(s string) error { return errors.New(s) }
func parseObject(b []byte) (map[string]any, error) {
	if !utf8.Valid(b) {
		return nil, fail("invalid UTF-8 JSON")
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	v, e := parseValue(d)
	if e != nil {
		return nil, e
	}
	if _, e = d.Token(); e != io.EOF {
		return nil, fail("trailing JSON content")
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fail("JSON object required")
	}
	return m, nil
}
func parseValue(d *json.Decoder) (any, error) {
	t, e := d.Token()
	if e != nil {
		return nil, e
	}
	if delim, ok := t.(json.Delim); ok {
		switch delim {
		case '{':
			m := map[string]any{}
			for d.More() {
				k, e := d.Token()
				if e != nil {
					return nil, e
				}
				s, ok := k.(string)
				if !ok {
					return nil, fail("invalid object key")
				}
				if _, ok = m[s]; ok {
					return nil, fail("duplicate JSON object key: " + s)
				}
				v, e := parseValue(d)
				if e != nil {
					return nil, e
				}
				m[s] = v
			}
			_, e = d.Token()
			return m, e
		case '[':
			a := []any{}
			for d.More() {
				v, e := parseValue(d)
				if e != nil {
					return nil, e
				}
				a = append(a, v)
			}
			_, e = d.Token()
			return a, e
		}
		return nil, fail("invalid JSON delimiter")
	}
	return t, nil
}

// Canonical uses Python's UTF-8, sorted, compact JSON convention.
func Canonical(v any) ([]byte, error) {
	var b bytes.Buffer
	if e := canonicalValue(&b, v); e != nil {
		return nil, e
	}
	return b.Bytes(), nil
}
func canonicalValue(b *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			writeString(b, k)
			b.WriteByte(':')
			if e := canonicalValue(b, x[k]); e != nil {
				return e
			}
		}
		b.WriteByte('}')
	case []any:
		b.WriteByte('[')
		for i, z := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if e := canonicalValue(b, z); e != nil {
				return e
			}
		}
		b.WriteByte(']')
	case string:
		writeString(b, x)
	case json.Number:
		if strings.ContainsAny(string(x), ".eE") {
			f, e := strconv.ParseFloat(string(x), 64)
			if e != nil {
				return e
			}
			return canonicalValue(b, f)
		}
		if _, ok := newBigInt(string(x)); !ok {
			return fail("invalid JSON integer")
		}
		b.WriteString(string(x))
	case float64:
		if math.IsInf(x, 0) || math.IsNaN(x) {
			return fail("non-finite JSON number")
		}
		s := strconv.FormatFloat(x, 'g', -1, 64)
		if !strings.ContainsAny(s, ".e") {
			s += ".0"
		}
		if a := strings.IndexByte(s, 'e'); a >= 0 {
			ex, _ := strconv.Atoi(s[a+1:])
			if ex >= -4 && ex < 16 {
				s = strconv.FormatFloat(x, 'f', -1, 64)
				if !strings.Contains(s, ".") {
					s += ".0"
				}
			}
		}
		b.WriteString(s)
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	default:
		raw, e := json.Marshal(v)
		if e != nil {
			return e
		}
		d := json.NewDecoder(bytes.NewReader(raw))
		d.UseNumber()
		var y any
		if e = d.Decode(&y); e != nil {
			return e
		}
		return canonicalValue(b, y)
	}
	return nil
}
func writeString(b *bytes.Buffer, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

func exact(m map[string]any, keys ...string) error {
	if m == nil || len(m) != len(keys) {
		return fail("invalid object fields")
	}
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			return fmt.Errorf("missing field %s", k)
		}
	}
	return nil
}
func object(v any) (map[string]any, error) {
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fail("expected object")
	}
	return m, nil
}
func int64Value(v any) (int64, error) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, fail("expected JSON integer")
	}
	i, e := n.Int64()
	return i, e
}
func artifact(v any) bool {
	s, ok := v.(string)
	return ok && strings.HasPrefix(s, "artifact://sha256/") && digestPattern.MatchString(strings.TrimPrefix(s, "artifact://sha256/"))
}
func Decode(b []byte) (*Request, error) {
	m, e := parseObject(b)
	if e != nil {
		return nil, e
	}
	v, e := int64Value(m["schema_version"])
	if e != nil || v < 5 || v > 6 {
		return nil, fail("native requests require schema_version 5 or 6; legacy requests require the stateless adapter")
	}
	keys := []string{"schema_version", "idempotency_key", "backend", "evidence_tier", "inputs", "config", "execution", "budgets"}
	if _, ok := m["quant_run_manifest_ref"]; ok {
		keys = append(keys, "quant_run_manifest_ref")
		if !artifact(m["quant_run_manifest_ref"]) {
			return nil, fail("invalid quant_run_manifest_ref")
		}
	}
	if e = exact(m, keys...); e != nil {
		return nil, e
	}
	k, ok := m["idempotency_key"].(string)
	if !ok || !keyPattern.MatchString(k) {
		return nil, fail("invalid idempotency_key")
	}
	backend := "nautilus.quote_market_orders"
	if v == 6 {
		backend = "nautilus.session_replay"
	}
	if m["backend"] != backend || m["evidence_tier"] != "diagnostic" {
		return nil, fail("invalid backend or evidence_tier")
	}
	in, e := object(m["inputs"])
	if e != nil {
		return nil, e
	}
	if e = exact(in, "engine_request_ref"); e != nil || !artifact(in["engine_request_ref"]) {
		return nil, fail("invalid engine inputs")
	}
	c, e := object(m["config"])
	if e != nil {
		return nil, e
	}
	s, ok := c["executable_sha256"].(string)
	if exact(c, "executable_sha256") != nil || !ok || !digestPattern.MatchString(s) {
		return nil, fail("invalid executable_sha256")
	}
	ex, e := object(m["execution"])
	if e != nil || exact(ex) != nil {
		return nil, fail("execution must be empty")
	}
	if _, e = readBudgets(m["budgets"]); e != nil {
		return nil, e
	}
	raw, e := Canonical(m)
	if e != nil {
		return nil, e
	}
	h := sha256.Sum256(raw)
	return importMapping(m, string(raw), hex.EncodeToString(h[:]))
}
func readBudgets(v any) (Budgets, error) {
	m, e := object(v)
	if e != nil {
		return Budgets{}, e
	}
	if e = exact(m, "wall_seconds", "memory_mb"); e != nil {
		return Budgets{}, e
	}
	w, e := int64Value(m["wall_seconds"])
	if e != nil || w < 1 || w > 86400 {
		return Budgets{}, fail("invalid wall_seconds")
	}
	mem, e := int64Value(m["memory_mb"])
	if e != nil || mem < 256 || mem > 65536 {
		return Budgets{}, fail("invalid memory_mb")
	}
	return Budgets{int(w), int(mem)}, nil
}

// ImportCanonical verifies an adapter's trusted normalization receipt without re-encoding it.
func ImportCanonical(canonical, digest string) (*Request, error) {
	h := sha256.Sum256([]byte(canonical))
	if !digestPattern.MatchString(digest) || hex.EncodeToString(h[:]) != digest {
		return nil, fail("canonical request digest mismatch")
	}
	m, e := parseObject([]byte(canonical))
	if e != nil {
		return nil, e
	}
	return importMapping(m, canonical, digest)
}
func importMapping(m map[string]any, raw, digest string) (*Request, error) {
	if len(raw) > 65536 {
		return nil, fail("request exceeds 64 KiB")
	}
	v, e := int64Value(m["schema_version"])
	if e != nil || v < 1 || v > 6 {
		return nil, fail("invalid schema_version")
	}
	k, ok := m["idempotency_key"].(string)
	if !ok || !keyPattern.MatchString(k) {
		return nil, fail("invalid idempotency key")
	}
	bud, e := readBudgets(m["budgets"])
	if e != nil {
		return nil, e
	}
	in, e := object(m["inputs"])
	if e != nil {
		return nil, e
	}
	inputs := map[string]string{}
	for k, z := range in {
		s, ok := z.(string)
		if !ok || !artifact(s) {
			return nil, fail("invalid artifact reference")
		}
		inputs[k] = s
	}
	c, e := object(m["config"])
	if e != nil {
		return nil, e
	}
	ex, e := object(m["execution"])
	if e != nil {
		return nil, e
	}
	backend, ok := m["backend"].(string)
	if !ok {
		return nil, fail("invalid backend")
	}
	tier, ok := m["evidence_tier"].(string)
	if !ok {
		return nil, fail("invalid evidence tier")
	}
	return &Request{int(v), k, backend, tier, inputs, c, ex, m, bud, raw, digest}, nil
}

// DecodeObject parses one strict JSON object, rejects duplicate keys at every
// depth, and preserves number tokens without a floating-point conversion.
func DecodeObject(data []byte) (map[string]any, error) { return parseObject(data) }
