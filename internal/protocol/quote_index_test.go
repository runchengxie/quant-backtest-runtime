package protocol

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestQuoteClockIndexPreservesDuplicatesAndInstrumentIdentity(t *testing.T) {
	quotes := []map[string]any{
		{"symbol": "AAA", "ts_ns": json.Number("10"), "bid": "1", "ask": "1"},
		{"symbol": "BBB", "ts_ns": json.Number("20"), "bid": "1", "ask": "1"},
		{"symbol": "AAA", "ts_ns": json.Number("10"), "bid": "2", "ask": "2"},
	}
	clocks, last := indexQuoteClocks(quotes)
	if !clocks[quoteClock{"AAA", 10}] || clocks[quoteClock{"AAA", 20}] || clocks[quoteClock{"BBB", 10}] {
		t.Fatal("quote clocks must bind both instrument and timestamp")
	}
	if len(clocks) != 2 || last["AAA"]["bid"] != "2" {
		t.Fatal("duplicates must remain accepted and last equal-clock quote must supply mark")
	}
	// A duplicate clock must still validate every quote's price, even when indexed.
	quotes[0]["bid"] = "invalid"
	defer func() {
		if _, ok := recover().(invalidEvidence); !ok {
			t.Fatal("invalid duplicate quote accepted")
		}
	}()
	indexQuoteClocks(quotes)
}

func BenchmarkQuoteClockLookup(b *testing.B) {
	for _, size := range []int{100, 1000, 10000} {
		b.Run(fmt.Sprint(size), func(b *testing.B) {
			quotes := make([]map[string]any, size)
			for i := range quotes {
				quotes[i] = map[string]any{"symbol": "AAA", "ts_ns": json.Number(fmt.Sprint(i + 1)), "bid": "1", "ask": "1"}
			}
			b.ResetTimer()
			for n := 0; n < b.N; n++ {
				clocks, _ := indexQuoteClocks(quotes)
				for i := 0; i < size; i++ {
					if !clocks[quoteClock{"AAA", int64(i + 1)}] {
						b.Fatal("missing clock")
					}
				}
			}
		})
	}
}

func TestV5QuoteClockBindingAndDuplicateMarks(t *testing.T) {
	original, result := v5Fixture()
	for _, tc := range []struct {
		name      string
		quotes    []any
		wantError string
	}{
		{"other-instrument-same-clock", []any{map[string]any{"symbol": "BBB", "ts_ns": json.Number("1735689600000000000"), "bid": "2", "ask": "2"}}, "fill has no input quote clock"},
		{"last-equal-clock-mark", []any{
			map[string]any{"symbol": "AAA", "ts_ns": json.Number("1735689600000000000"), "bid": "1", "ask": "1"},
			map[string]any{"symbol": "AAA", "ts_ns": json.Number("1735689600000000000"), "bid": "2", "ask": "2"},
		}, ""},
		{"invalid-duplicate", []any{
			map[string]any{"symbol": "AAA", "ts_ns": json.Number("1735689600000000000"), "bid": "invalid", "ask": "1"},
			map[string]any{"symbol": "AAA", "ts_ns": json.Number("1735689600000000000"), "bid": "2", "ask": "2"},
		}, "invalid positive decimal"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			input, err := parseObject(original)
			if err != nil {
				t.Fatal(err)
			}
			input["quotes"] = tc.quotes
			data, err := Canonical(input)
			if err != nil {
				t.Fatal(err)
			}
			_, err = ValidateResult(nativeRequest(t, 5, data), data, result)
			if tc.wantError == "" {
				if err != nil {
					t.Fatal(err)
				}
			} else if err == nil || !strings.Contains(err.Error(), tc.wantError) {
				t.Fatalf("expected %q, got %v", tc.wantError, err)
			}
		})
	}
}

// The reference loop is intentionally retained only in this benchmark: matching
// N fills against N quotes took N*N identity comparisons before indexing.
func BenchmarkQuoteClockReferenceScan(b *testing.B) {
	for _, size := range []int{100, 1000, 10000} {
		b.Run(fmt.Sprint(size), func(b *testing.B) {
			quotes := make([]map[string]any, size)
			for i := range quotes {
				quotes[i] = map[string]any{"symbol": "AAA", "ts_ns": json.Number(fmt.Sprint(i + 1))}
			}
			b.ResetTimer()
			for n := 0; n < b.N; n++ {
				for i := 0; i < size; i++ {
					found := false
					for _, q := range quotes {
						if q["symbol"] == "AAA" && integer(q["ts_ns"]) == int64(i+1) {
							found = true
						}
					}
					if !found {
						b.Fatal("missing clock")
					}
				}
			}
		})
	}
}
