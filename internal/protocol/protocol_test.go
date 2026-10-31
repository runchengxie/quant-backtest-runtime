package protocol

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func nativeRequest(t *testing.T, version int, input []byte) *Request {
	t.Helper()
	h := sha256.Sum256(input)
	b, _ := json.Marshal(map[string]any{"schema_version": version, "idempotency_key": "synthetic", "backend": map[int]string{5: "nautilus.quote_market_orders", 6: "nautilus.session_replay"}[version], "evidence_tier": "diagnostic", "inputs": map[string]string{"engine_request_ref": "artifact://sha256/" + hex.EncodeToString(h[:])}, "config": map[string]string{"executable_sha256": strings.Repeat("a", 64)}, "execution": map[string]any{}, "budgets": map[string]int{"wall_seconds": 10, "memory_mb": 4096}})
	r, e := Decode(b)
	if e != nil {
		t.Fatal(e)
	}
	return r
}
func fixture(t *testing.T) ([]byte, []byte) {
	t.Helper()
	i, e := os.ReadFile("testdata/session_input.json")
	if e != nil {
		t.Fatal(e)
	}
	r, e := os.ReadFile("testdata/session_result.json")
	if e != nil {
		t.Fatal(e)
	}
	return i, r
}
func TestSessionIntegrity(t *testing.T) {
	i, b := fixture(t)
	r := nativeRequest(t, 6, i)
	if _, e := ValidateResult(r, i, b); e != nil {
		t.Fatal(e)
	}
	mutations := map[string]func(map[string]any){
		"cash": func(v map[string]any) {
			d := v["daily_ledger"].([]any)[0].(map[string]any)
			d["cash"] = "8"
			d["nav"] = "10"
		},
		"mark": func(v map[string]any) {
			p := v["daily_positions"].([]any)[1].(map[string]any)
			p["mark_price"] = "999"
			p["market_value"] = "999"
			d := v["daily_ledger"].([]any)[1].(map[string]any)
			d["positions_value"] = "999"
			d["nav"] = "1006.99"
			v["final_nav"] = "1006.99"
		},
		"fill-clock": func(v map[string]any) {
			v["fills"].([]any)[0].(map[string]any)["ts_ns"] = json.Number("1735689599999999999")
		},
		"quantity": func(v map[string]any) { v["daily_positions"].([]any)[0].(map[string]any)["quantity"] = "2" },
		"fee":      func(v map[string]any) { v["total_commission"] = "0.02" },
		"rejection": func(v map[string]any) {
			v["rejections"] = []any{map[string]any{"order_id": "buy", "reason": "fake", "event_type": "denied"}}
		},
		"nonterminal": func(v map[string]any) { v["orders"].([]any)[0].(map[string]any)["status"] = "ACCEPTED" },
	}
	for n, f := range mutations {
		t.Run(n, func(t *testing.T) {
			v, e := parseObject(b)
			if e != nil {
				t.Fatal(e)
			}
			f(v)
			bad, _ := Canonical(v)
			if _, e = ValidateResult(r, i, bad); e == nil {
				t.Fatal("accepted tampered evidence")
			}
		})
	}
	if _, e := ValidateResult(r, append(i, ' '), b); e == nil {
		t.Fatal("accepted input digest mismatch")
	}
}
func TestRejectDuplicateJSON(t *testing.T) {
	if _, e := Decode([]byte(`{"schema_version":5,"schema_version":6}`)); e == nil {
		t.Fatal("duplicate accepted")
	}
}
func TestCanonicalUnicode(t *testing.T) {
	b, e := Canonical(map[string]any{"z": "<>&\u2028中文", "a": 1})
	if e != nil {
		t.Fatal(e)
	}
	if string(b) != "{\"a\":1,\"z\":\"<>&\u2028中文\"}" {
		t.Fatalf("%s", b)
	}
}
func TestExactDecimals(t *testing.T) {
	if _, e := decimal("1000000000000000000.00000001", false, false); e == nil {
		t.Fatal("above bound")
	}
	if _, e := decimal("NaN", false, false); e == nil {
		t.Fatal("NaN")
	}
}

func TestSessionT1(t *testing.T) {
	i, b := fixture(t)
	v, e := parseObject(b)
	if e != nil {
		t.Fatal(e)
	}
	v["orders"] = append(v["orders"].([]any), map[string]any{"id": "sell", "symbol": "AAA", "status": "FILLED", "filled_quantity": "1"})
	v["fills"] = append(v["fills"].([]any), map[string]any{"order_id": "sell", "symbol": "AAA", "side": "sell", "ts_ns": json.Number("1735689600000000001"), "quantity": "1", "price": "2", "commission": "0"})
	v["daily_positions"] = []any{}
	for _, x := range v["daily_ledger"].([]any) {
		d := x.(map[string]any)
		d["cash"] = "9.99"
		d["nav"] = "9.99"
		d["positions_value"] = "0"
	}
	v["final_cash"] = "9.99"
	v["final_nav"] = "9.99"
	bad, _ := Canonical(v)
	r := nativeRequest(t, 6, i)
	if _, e := ValidateResult(r, i, bad); e == nil || !strings.Contains(e.Error(), "T+1") {
		t.Fatalf("expected T+1 failure, got %v", e)
	}
}
func TestRoundCentHalfEven(t *testing.T) {
	for _, tc := range []struct{ input, want string }{{"2.005", "2.00"}, {"2.015", "2.02"}, {"2.025", "2.02"}, {"999999999999999999.995", "1000000000000000000.00"}} {
		n, e := decimal(tc.input, false, false)
		if e != nil {
			t.Fatal(e)
		}
		w, e := decimal(tc.want, false, false)
		if e != nil {
			t.Fatal(e)
		}
		if !equal(roundCent(n), w) {
			t.Fatal(tc)
		}
	}
}
func TestV5FlowAndInputBinding(t *testing.T) {
	i := []byte(`{"schema_version":"engine_request.v1","initial_cash":"10","quotes":[{"symbol":"AAA","ts_ns":1735689600000000000,"bid":"2","ask":"2"}],"orders":[{"id":"b","symbol":"AAA","information_ts_ns":1735689599999999999,"side":"buy","quantity":"1"}]}`)
	b := []byte(`{"schema_version":"engine_result.v1","backend":"nautilus","backend_version":"0.66.0","currency":"USD","capabilities":["quote_market_orders","multi_asset","final_balances","proportional_fees"],"cash":"7.99","positions_value":"2","equity":"9.99","commission":"0.01","orders":[{"id":"b","symbol":"AAA","status":"FILLED","filled_quantity":"1"}],"fills":[{"order_id":"b","symbol":"AAA","side":"buy","ts_ns":1735689600000000000,"quantity":"1","price":"2","commission":"0.01"}],"positions":[{"symbol":"AAA","quantity":"1","mark_price":"2","market_value":"2"}]}`)
	r := nativeRequest(t, 5, i)
	v, e := ValidateResult(r, i, b)
	if e != nil {
		t.Fatal(e)
	}
	if Summary(r, v)["equity"] != "9.99" {
		t.Fatal("summary")
	}
	for _, tc := range []struct{ old, new string }{{`"cash":"7.99"`, `"cash":"8.99"`}, {`"mark_price":"2"`, `"mark_price":"3"`}, {`"side":"buy"`, `"side":"sell"`}, {`"filled_quantity":"1"`, `"filled_quantity":"2"`}} {
		bad := strings.Replace(string(b), tc.old, tc.new, 1)
		if _, e = ValidateResult(r, i, []byte(bad)); e == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
}
func TestRequestStrictness(t *testing.T) {
	r := nativeRequest(t, 5, []byte("{}"))
	base := r.CanonicalJSON
	for _, tc := range []struct{ old, new string }{{`"wall_seconds":10`, `"wall_seconds":0`}, {`"memory_mb":4096`, `"memory_mb":255`}, {`"wall_seconds":10`, `"wall_seconds":10.0`}, {`"execution":{}`, `"execution":{"shell":true}`}, {`"schema_version":5`, `"schema_version":4`}, {`"diagnostic"`, `"execution_aware"`}} {
		if _, e := Decode([]byte(strings.Replace(base, tc.old, tc.new, 1))); e == nil {
			t.Fatal(tc)
		}
	}
	if _, e := ImportCanonical(base, strings.Repeat("f", 64)); e == nil {
		t.Fatal("bad receipt hash")
	}
	imported, e := ImportCanonical(base, r.SHA256)
	if e != nil || imported.SHA256 != r.SHA256 {
		t.Fatal(e)
	}
}

// Python is a differential oracle in tests only; the production package never invokes it.
func TestPythonCanonicalDifferential(t *testing.T) {
	python, e := exec.LookPath("python3")
	if e != nil {
		t.Skip("Python comparison interpreter unavailable")
	}
	values := []string{`{"text":"中文<>&\u2028","x":1.0,"small":0.00001,"fixed":1e15,"big":1e16,"negative_zero":-0.0}`, `{"integer":1735689599999999999,"array":[null,true,false,"é"]}`}
	for _, source := range values {
		m, e := parseObject([]byte(source))
		if e != nil {
			t.Fatal(e)
		}
		got, e := Canonical(m)
		if e != nil {
			t.Fatal(e)
		}
		cmd := exec.Command(python, "-c", `import sys,json;sys.stdout.write(json.dumps(json.load(sys.stdin),ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False))`)
		cmd.Stdin = strings.NewReader(source)
		want, e := cmd.Output()
		if e != nil {
			t.Fatal(e)
		}
		if !bytes.Equal(got, want) {
			t.Fatalf("canonical mismatch\nGo: %s\nPython: %s", got, want)
		}
	}
	r := nativeRequest(t, 6, []byte("{}"))
	cmd := exec.Command(python, "-c", `import json,sys;from backtest_runtime.contracts import BacktestJobRequest;r=BacktestJobRequest.from_mapping(json.load(sys.stdin));print(r.request_sha256)`)
	sourceRoot, e := filepath.Abs("../../src")
	if e != nil {
		t.Fatal(e)
	}
	cmd.Env = append(os.Environ(), "PYTHONPATH="+sourceRoot)
	cmd.Stdin = strings.NewReader(r.CanonicalJSON)
	want, e := cmd.CombinedOutput()
	if e != nil {
		t.Fatalf("Python owner: %v: %s", e, want)
	}
	if strings.TrimSpace(string(want)) != r.SHA256 {
		t.Fatalf("hash mismatch %s / %s", r.SHA256, want)
	}
}
func TestPythonSessionDifferential(t *testing.T) {
	python, e := exec.LookPath("python3")
	if e != nil {
		t.Skip("Python comparison interpreter unavailable")
	}
	i, b := fixture(t)
	r := nativeRequest(t, 6, i)
	cases := []map[string]any{}
	for _, mutation := range []func(map[string]any){func(v map[string]any) {}, func(v map[string]any) { v["final_cash"] = "8" }, func(v map[string]any) { v["daily_ledger"].([]any)[0].(map[string]any)["commission"] = "1" }, func(v map[string]any) {
		v["fills"].([]any)[0].(map[string]any)["ts_ns"] = json.Number("1735689599999999999")
	}, func(v map[string]any) { v["daily_positions"].([]any)[0].(map[string]any)["mark_price"] = "3" }} {
		v, _ := parseObject(b)
		mutation(v)
		cases = append(cases, v)
	}
	data, _ := json.Marshal(map[string]any{"input": json.RawMessage(i), "cases": cases})
	cmd := exec.Command(python, "-c", `import json,sys
from backtest_runtime.engine_sessions_protocol import validate_session_result,bind_session_request
v=json.load(sys.stdin);out=[]
for r in v["cases"]:
 try: validate_session_result(r);bind_session_request(r,v["input"]);out.append(True)
 except (ValueError,TypeError,KeyError): out.append(False)
print(json.dumps(out))`)
	sourceRoot, _ := filepath.Abs("../../src")
	cmd.Env = append(os.Environ(), "PYTHONPATH="+sourceRoot)
	cmd.Stdin = bytes.NewReader(data)
	out, e := cmd.CombinedOutput()
	if e != nil {
		t.Fatalf("%v %s", e, out)
	}
	var want []bool
	if e = json.Unmarshal(out, &want); e != nil {
		t.Fatal(e)
	}
	for idx, v := range cases {
		data, _ := Canonical(v)
		_, e := ValidateResult(r, i, data)
		if (e == nil) != want[idx] {
			t.Fatalf("Python differential case %d: Python=%v Go error=%v", idx, want[idx], e)
		}
	}
}
func TestCanonicalLiteralUnicodeEscape(t *testing.T) {
	v := map[string]any{"text": `\u2028 literal and actual ` + "\u2028"}
	b, e := Canonical(v)
	if e != nil {
		t.Fatal(e)
	}
	var round map[string]any
	if e = json.Unmarshal(b, &round); e != nil {
		t.Fatalf("invalid canonical JSON: %s: %v", b, e)
	}
	if round["text"] != v["text"] {
		t.Fatalf("roundtrip mismatch: %s", b)
	}
}
func TestRealEngine(t *testing.T) {
	binary := os.Getenv("BACKTEST_ENGINE_TEST_EXECUTABLE")
	if binary == "" {
		t.Skip("set BACKTEST_ENGINE_TEST_EXECUTABLE for immutable engine integration")
	}
	for _, v := range []int{5, 6} {
		t.Run(fmt.Sprint(v), func(t *testing.T) {
			path := "testdata/engine_v" + fmt.Sprint(v) + "_input.json"
			input, e := os.ReadFile(path)
			if e != nil {
				t.Fatal(e)
			}
			out, e := exec.Command(binary, "run", path).CombinedOutput()
			if e != nil {
				t.Fatalf("engine: %v %s", e, out)
			}
			r := nativeRequest(t, v, input)
			result, e := ValidateResult(r, input, out)
			if e != nil {
				t.Fatalf("%v\n%s", e, out)
			}
			if Summary(r, result)["equity"] == nil {
				t.Fatal("missing financial summary")
			}
		})
	}
}
func FuzzMalformedEvidence(f *testing.F) {
	input, e := os.ReadFile("testdata/session_input.json")
	if e != nil {
		f.Fatal(e)
	}
	result, e := os.ReadFile("testdata/session_result.json")
	if e != nil {
		f.Fatal(e)
	}
	h := sha256.Sum256(input)
	r := &Request{SchemaVersion: 6, Inputs: map[string]string{"engine_request_ref": "artifact://sha256/" + hex.EncodeToString(h[:])}}
	f.Add(result)
	f.Add([]byte(`{"orders":null}`))
	f.Fuzz(func(t *testing.T, b []byte) { _, _ = ValidateResult(r, input, b) })
}
func TestDecodeObjectStrict(t *testing.T) {
	if _, e := DecodeObject([]byte(`{"x":1,"x":2}`)); e == nil {
		t.Fatal("duplicate key")
	}
	if _, e := DecodeObject([]byte(`{"x":NaN}`)); e == nil {
		t.Fatal("non-finite number")
	}
	if _, e := DecodeObject([]byte(`[]`)); e == nil {
		t.Fatal("non-object")
	}
	m, e := DecodeObject([]byte(`{"ts":1735689599999999999}`))
	if e != nil {
		t.Fatal(e)
	}
	if m["ts"] != json.Number("1735689599999999999") {
		t.Fatal("integer lost precision")
	}
}
