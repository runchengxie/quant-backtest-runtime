package protocol

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math/big"
	"regexp"
	"strings"
	"time"
)

var decimalPattern = regexp.MustCompile(`^[0-9]+(?:\.[0-9]{1,8})?$`)
var identifierPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,40}$`)

func newBigInt(s string) (*big.Int, bool) { return new(big.Int).SetString(s, 10) }
func decimal(v any, positive, integer bool) (*big.Rat, error) {
	s, ok := v.(string)
	if !ok || len(s) > 28 || !decimalPattern.MatchString(s) {
		return nil, fail("financial values must be bounded decimal strings")
	}
	n, ok := new(big.Rat).SetString(s)
	if !ok || n.Cmp(new(big.Rat).SetInt64(1000000000000000000)) > 0 || (positive && n.Sign() == 0) || (integer && !n.IsInt()) {
		return nil, fail("decimal outside supported bounds")
	}
	return n, nil
}

type invalidEvidence struct{ message string }

func require(ok bool, message string) {
	if !ok {
		panic(invalidEvidence{message})
	}
}
func mustObj(v any) map[string]any {
	m, e := object(v)
	require(e == nil, "expected JSON object")
	return m
}
func str(v any) string { s, ok := v.(string); require(ok, "expected string"); return s }
func dec(v any) *big.Rat {
	n, e := decimal(v, false, false)
	require(e == nil, "invalid financial decimal")
	return n
}
func qty(v any, positive bool) *big.Rat {
	n, e := decimal(v, positive, true)
	require(e == nil, "invalid integer quantity")
	return n
}
func positive(v any) *big.Rat {
	n, e := decimal(v, true, false)
	require(e == nil, "invalid positive decimal")
	return n
}
func integer(v any) int64 { n, e := int64Value(v); require(e == nil, "expected integer"); return n }
func timestamp(v any) int64 {
	n := integer(v)
	require(n > 0 && n <= 4102444800000000000, "invalid timestamp")
	return n
}
func ident(v any) string {
	s := str(v)
	require(identifierPattern.MatchString(s), "invalid identifier")
	return s
}
func boolean(v any) bool { x, ok := v.(bool); require(ok, "expected boolean"); return x }
func rows(v any, keys ...string) []map[string]any {
	a, ok := v.([]any)
	require(ok, "expected record array")
	r := make([]map[string]any, 0, len(a))
	for _, x := range a {
		m := mustObj(x)
		if keys != nil {
			require(exact(m, keys...) == nil, "invalid record fields")
		}
		r = append(r, m)
	}
	return r
}
func add(a, b *big.Rat) *big.Rat { return new(big.Rat).Add(a, b) }
func sub(a, b *big.Rat) *big.Rat { return new(big.Rat).Sub(a, b) }
func mul(a, b *big.Rat) *big.Rat { return new(big.Rat).Mul(a, b) }
func equal(a, b *big.Rat) bool   { return a.Cmp(b) == 0 }
func zero() *big.Rat             { return new(big.Rat) }
func lookup(m map[string]*big.Rat, k string) *big.Rat {
	if n := m[k]; n != nil {
		return n
	}
	return zero()
}
func capabilities(v any, want ...string) {
	a, ok := v.([]any)
	require(ok && len(a) == len(want), "invalid capabilities")
	seen := map[string]bool{}
	for _, x := range a {
		s := str(x)
		require(!seen[s], "duplicate capability")
		seen[s] = true
	}
	for _, s := range want {
		require(seen[s], "missing capability")
	}
}

var orderKeys = []string{"id", "symbol", "status", "filled_quantity"}
var fillKeys = []string{"order_id", "symbol", "side", "ts_ns", "quantity", "price", "commission"}
var positionKeys = []string{"symbol", "quantity", "mark_price", "market_value"}
var statuses = map[string]bool{"INITIALIZED": true, "SUBMITTED": true, "ACCEPTED": true, "PARTIALLY_FILLED": true, "PENDING_CANCEL": true, "PENDING_UPDATE": true, "CANCELED": true, "FILLED": true, "DENIED": true, "REJECTED": true, "EXPIRED": true, "NOT_SUBMITTED": true}
var terminal = map[string]bool{"FILLED": true, "CANCELED": true, "DENIED": true, "REJECTED": true, "NOT_SUBMITTED": true, "EXPIRED": true}

func validateOrders(v any, session bool) map[string]map[string]any {
	out := map[string]map[string]any{}
	for _, r := range rows(v, orderKeys...) {
		id := ident(r["id"])
		ident(r["symbol"])
		s := str(r["status"])
		require(statuses[s] && out[id] == nil, "invalid order identity/status")
		require(!session || terminal[s], "nonterminal session order")
		q := qty(r["filled_quantity"], false)
		if s == "DENIED" || s == "REJECTED" || s == "NOT_SUBMITTED" {
			require(q.Sign() == 0, "unexecuted order has fills")
		}
		out[id] = r
	}
	return out
}
func validateFills(v any, orders map[string]map[string]any) ([]map[string]any, *big.Rat) {
	fs := rows(v, fillKeys...)
	quantities := map[string]*big.Rat{}
	fee := zero()
	for _, f := range fs {
		id := ident(f["order_id"])
		o := orders[id]
		require(o != nil && f["symbol"] == o["symbol"], "fill references unknown order/symbol")
		require(f["side"] == "buy" || f["side"] == "sell", "invalid fill side")
		timestamp(f["ts_ns"])
		q := qty(f["quantity"], true)
		positive(f["price"])
		fee = add(fee, dec(f["commission"]))
		quantities[id] = add(lookup(quantities, id), q)
	}
	for id, o := range orders {
		require(equal(lookup(quantities, id), dec(o["filled_quantity"])), "fill quantities do not reconcile")
	}
	return fs, fee
}
func positions(rs []map[string]any) (*big.Rat, map[string]*big.Rat) {
	total := zero()
	quantities := map[string]*big.Rat{}
	for _, p := range rs {
		s := ident(p["symbol"])
		require(quantities[s] == nil, "duplicate position")
		q := qty(p["quantity"], true)
		mark := positive(p["mark_price"])
		v := dec(p["market_value"])
		require(equal(mul(q, mark), v), "position mark does not reconcile")
		quantities[s] = q
		total = add(total, v)
	}
	return total, quantities
}

// ValidateResult validates evidence and binds it to the exact content-addressed input.
// Legacy financial validation is intentionally owned by the stateless adapter.
func ValidateResult(req *Request, inputBytes, resultBytes []byte) (result map[string]any, err error) {
	defer func() {
		if p := recover(); p != nil {
			if v, ok := p.(invalidEvidence); ok {
				result = nil
				err = fmt.Errorf("invalid engine evidence: %s", v.message)
			} else {
				panic(p)
			}
		}
	}()
	require(req != nil, "missing request")
	require(req.SchemaVersion == 5 || req.SchemaVersion == 6, "legacy evidence requires stateless adapter")
	h := sha256.Sum256(inputBytes)
	require(req.Inputs["engine_request_ref"] == "artifact://sha256/"+hex.EncodeToString(h[:]), "input artifact digest mismatch")
	in, e := parseObject(inputBytes)
	if e != nil {
		return nil, e
	}
	result, e = parseObject(resultBytes)
	if e != nil {
		return nil, e
	}
	if req.SchemaVersion == 6 {
		validateSession(in, result)
	} else {
		validateEngine(in, result)
	}
	return result, nil
}
func validateEngine(in, r map[string]any) {
	require(exact(r, "schema_version", "backend", "backend_version", "capabilities", "currency", "cash", "positions_value", "equity", "commission", "orders", "fills", "positions") == nil, "invalid engine result fields")
	require(r["schema_version"] == "engine_result.v1" && r["backend"] == "nautilus" && r["backend_version"] == "0.66.0" && r["currency"] == "USD", "unsupported engine identity")
	capabilities(r["capabilities"], "quote_market_orders", "multi_asset", "final_balances", "proportional_fees")
	cash, value, equity, commission := dec(r["cash"]), dec(r["positions_value"]), dec(r["equity"]), dec(r["commission"])
	total, holding := positions(rows(r["positions"], positionKeys...))
	require(equal(add(cash, value), equity) && equal(total, value), "engine equity does not reconcile")
	orders := validateOrders(r["orders"], false)
	fills, fee := validateFills(r["fills"], orders)
	require(equal(fee, commission), "engine commission does not reconcile")
	require(in["schema_version"] == "engine_request.v1", "v5 requires engine_request.v1")
	intents := intentMap(rows(in["orders"]))
	bindOrders(orders, intents)
	quotes := rows(in["quotes"])
	last := map[string]map[string]any{}
	for _, q := range quotes {
		s := ident(q["symbol"])
		timestamp(q["ts_ns"])
		positive(q["bid"])
		positive(q["ask"])
		if old := last[s]; old == nil || integer(q["ts_ns"]) >= integer(old["ts_ns"]) {
			last[s] = q
		}
	}
	quantities := map[string]*big.Rat{}
	expectedCash := positive(in["initial_cash"])
	for _, f := range fills {
		o := intents[str(f["order_id"])]
		bindFillIdentity(f, o)
		ts := timestamp(f["ts_ns"])
		require(ts > integer(o["information_ts_ns"]), "fill/request clock mismatch")
		found := false
		for _, q := range quotes {
			if q["symbol"] == f["symbol"] && integer(q["ts_ns"]) == ts {
				found = true
			}
		}
		require(found, "fill has no input quote clock")
		s := str(f["symbol"])
		q := qty(f["quantity"], true)
		notional := roundCent(mul(q, positive(f["price"])))
		if f["side"] == "buy" {
			expectedCash = sub(expectedCash, notional)
			quantities[s] = add(lookup(quantities, s), q)
		} else {
			expectedCash = add(expectedCash, notional)
			quantities[s] = sub(lookup(quantities, s), q)
		}
		expectedCash = sub(expectedCash, dec(f["commission"]))
	}
	require(equal(expectedCash, cash), "engine cash flow does not reconcile")
	sameQuantities(quantities, holding)
	for _, p := range rows(r["positions"], positionKeys...) {
		q := last[str(p["symbol"])]
		require(q != nil, "missing input mark quote")
		mark := new(big.Rat).Quo(add(positive(q["bid"]), positive(q["ask"])), big.NewRat(2, 1))
		require(equal(mark, dec(p["mark_price"])), "engine input mark mismatch")
	}
}
func intentMap(rs []map[string]any) map[string]map[string]any {
	out := map[string]map[string]any{}
	for _, r := range rs {
		id := ident(r["id"])
		require(out[id] == nil, "duplicate input intent")
		ident(r["symbol"])
		qty(r["quantity"], true)
		require(r["side"] == "buy" || r["side"] == "sell", "invalid input side")
		integer(r["information_ts_ns"])
		out[id] = r
	}
	return out
}
func bindOrders(orders, intents map[string]map[string]any) {
	require(len(orders) == len(intents), "request/result order coverage mismatch")
	for id, o := range orders {
		i := intents[id]
		require(i != nil && o["symbol"] == i["symbol"], "order/request identity mismatch")
		filled, requested := dec(o["filled_quantity"]), qty(i["quantity"], true)
		require(filled.Cmp(requested) <= 0 && (o["status"] != "FILLED" || equal(filled, requested)), "order/request quantity mismatch")
	}
}
func bindFillIdentity(f, i map[string]any) {
	require(i != nil && f["symbol"] == i["symbol"] && f["side"] == i["side"], "fill/request identity mismatch")
}
func sameQuantities(expected, actual map[string]*big.Rat) {
	for s, q := range expected {
		require(equal(q, lookup(actual, s)), "fill/holding quantities do not reconcile")
	}
	for s, q := range actual {
		require(equal(q, lookup(expected, s)), "unexpected holding")
	}
}

// roundCent applies exact decimal ROUND_HALF_EVEN.
func roundCent(n *big.Rat) *big.Rat {
	scaled := mul(n, big.NewRat(100, 1))
	q, r := new(big.Int), new(big.Int)
	q.QuoRem(scaled.Num(), scaled.Denom(), r)
	cmp := new(big.Int).Mul(r, big.NewInt(2)).Cmp(scaled.Denom())
	if cmp > 0 || (cmp == 0 && q.Bit(0) == 1) {
		q.Add(q, big.NewInt(1))
	}
	return new(big.Rat).SetFrac(q, big.NewInt(100))
}
func sessionDate(ts int64, zone string) string {
	loc, e := time.LoadLocation(zone)
	require(e == nil, "unsupported valuation timezone")
	return time.Unix(ts/1000000000, 0).In(loc).Format("2006-01-02")
}
func validateSession(in, r map[string]any) {
	require(exact(r, "schema_version", "backend", "backend_version", "currency", "price_basis", "valuation_timezone", "enforce_t1", "initial_cash", "final_cash", "final_nav", "total_commission", "capabilities", "orders", "fills", "rejections", "daily_ledger", "daily_positions") == nil, "invalid session result fields")
	require(r["schema_version"] == "engine_result.v2" && r["backend"] == "nautilus" && r["backend_version"] == "0.66.0" && r["price_basis"] == "raw", "unsupported session identity")
	currency := str(r["currency"])
	require(currency == "USD" || currency == "CNY", "unsupported currency")
	zone := "UTC"
	if currency == "CNY" {
		zone = "Asia/Shanghai"
	}
	require(r["valuation_timezone"] == zone, "invalid session timezone")
	t1 := boolean(r["enforce_t1"])
	capabilities(r["capabilities"], "session_ledger", "configured_t_plus_one", "buy_lots", "quote_trade_permissions", "quote_liquidity")
	days := rows(r["daily_ledger"], "session_date", "valuation_ts_ns", "cash", "positions_value", "nav", "commission")
	require(len(days) > 0, "session balances empty")
	byDate := map[string]map[string]any{}
	previousDate := ""
	var previousTS int64
	for _, d := range days {
		label := str(d["session_date"])
		parsed, e := time.Parse("2006-01-02", label)
		require(e == nil && parsed.Format("2006-01-02") == label && label > previousDate, "invalid session date ordering")
		ts := timestamp(d["valuation_ts_ns"])
		require(ts > previousTS && sessionDate(ts, zone) == label, "invalid session valuation clock")
		require(equal(add(dec(d["cash"]), dec(d["positions_value"])), dec(d["nav"])), "session NAV mismatch")
		dec(d["commission"])
		byDate[label] = d
		previousDate = label
		previousTS = ts
	}
	holdings := map[string][]map[string]any{}
	for _, p := range rows(r["daily_positions"], "session_date", "symbol", "quantity", "mark_price", "market_value") {
		label := str(p["session_date"])
		require(byDate[label] != nil, "holding references unknown session")
		holdings[label] = append(holdings[label], p)
	}
	actual := map[string]map[string]*big.Rat{}
	for _, d := range days {
		label := str(d["session_date"])
		v, q := positions(holdings[label])
		require(equal(v, dec(d["positions_value"])), "session positions value mismatch")
		actual[label] = q
	}
	orders := validateOrders(r["orders"], true)
	fills, fee := validateFills(r["fills"], orders)
	require(equal(fee, dec(r["total_commission"])), "session total commission mismatch")
	seen := map[string]bool{}
	for _, x := range rows(r["rejections"], "order_id", "reason", "event_type") {
		id := ident(x["order_id"])
		o := orders[id]
		typ := str(x["event_type"])
		require(o != nil && !seen[id] && (typ == "denied" || typ == "rejected"), "invalid rejection identity")
		reason := str(x["reason"])
		require(o["status"] == strings.ToUpper(typ) && len([]rune(reason)) > 0 && len([]rune(reason)) <= 4096, "rejection/order mismatch")
		seen[id] = true
	}
	for id, o := range orders {
		require(seen[id] == (o["status"] == "DENIED" || o["status"] == "REJECTED"), "incomplete rejection evidence")
	}
	grouped := map[string][]map[string]any{}
	for _, f := range fills {
		ts := timestamp(f["ts_ns"])
		label := sessionDate(ts, zone)
		d := byDate[label]
		require(d != nil, "fill references unknown session")
		var start int64
		for i, x := range days {
			if x == nil {
				continue
			}
			if x["session_date"] == label && i > 0 {
				start = integer(days[i-1]["valuation_ts_ns"])
				break
			}
		}
		require(ts > start && ts <= integer(d["valuation_ts_ns"]), "fill outside session boundaries")
		grouped[label] = append(grouped[label], f)
	}
	cash := positive(r["initial_cash"])
	quantities := map[string]*big.Rat{}
	for _, d := range days {
		label := str(d["session_date"])
		fs := grouped[label]
		if t1 {
			sold := map[string]*big.Rat{}
			for _, f := range fs {
				if f["side"] == "sell" {
					s := str(f["symbol"])
					sold[s] = add(lookup(sold, s), dec(f["quantity"]))
				}
			}
			for s, q := range sold {
				require(q.Cmp(lookup(quantities, s)) <= 0, "session sells violate T+1")
			}
		}
		cost := zero()
		for _, f := range fs {
			s := str(f["symbol"])
			q := qty(f["quantity"], true)
			notional := roundCent(mul(q, positive(f["price"])))
			if f["side"] == "buy" {
				cash = sub(cash, notional)
				quantities[s] = add(lookup(quantities, s), q)
			} else {
				cash = add(cash, notional)
				quantities[s] = sub(lookup(quantities, s), q)
			}
			fee := dec(f["commission"])
			cash = sub(cash, fee)
			cost = add(cost, fee)
		}
		sameQuantities(quantities, actual[label])
		require(equal(cash, dec(d["cash"])) && equal(cost, dec(d["commission"])), "session fill/cash/fee flow mismatch")
	}
	last := days[len(days)-1]
	require(equal(dec(r["final_cash"]), dec(last["cash"])) && equal(dec(r["final_nav"]), dec(last["nav"])), "session final summary mismatch")
	bindSession(in, r, days, orders, fills, holdings)
}
func bindSession(in, r map[string]any, days []map[string]any, orders map[string]map[string]any, fills []map[string]any, holdings map[string][]map[string]any) {
	require(in["schema_version"] == "engine_request.v2", "v6 requires engine_request.v2")
	for _, k := range []string{"currency", "initial_cash", "price_basis", "enforce_t1"} {
		require(in[k] == r[k], "session result/request "+k+" mismatch")
	}
	sessions := rows(in["sessions"])
	require(len(sessions) == len(days), "session valuation coverage mismatch")
	allIntents := map[string]map[string]any{}
	intentSessions := map[string]map[string]any{}
	for idx, s := range sessions {
		d := days[idx]
		require(s["session_date"] == d["session_date"] && integer(s["valuation_ts_ns"]) == integer(d["valuation_ts_ns"]), "session/request valuation mismatch")
		open := timestamp(s["open_ts_ns"])
		require(open <= timestamp(s["valuation_ts_ns"]), "invalid input session interval")
		for id, o := range intentMap(rows(s["orders"])) {
			require(allIntents[id] == nil, "duplicate input intent")
			allIntents[id] = o
			intentSessions[id] = s
		}
		marks := map[string]map[string]any{}
		for _, q := range rows(s["quotes"]) {
			if integer(q["ts_ns"]) == integer(s["valuation_ts_ns"]) {
				marks[ident(q["symbol"])] = q
			}
		}
		for _, p := range holdings[str(d["session_date"])] {
			q := marks[str(p["symbol"])]
			require(q != nil, "missing session valuation quote")
			mark := new(big.Rat).Quo(add(positive(q["bid"]), positive(q["ask"])), big.NewRat(2, 1))
			require(equal(mark, dec(p["mark_price"])), "session valuation mark/request mismatch")
		}
	}
	bindOrders(orders, allIntents)
	for _, f := range fills {
		id := str(f["order_id"])
		o, s := allIntents[id], intentSessions[id]
		bindFillIdentity(f, o)
		ts := timestamp(f["ts_ns"])
		require(s != nil && ts >= integer(s["open_ts_ns"]) && ts <= integer(s["valuation_ts_ns"]) && ts > integer(o["information_ts_ns"]), "session fill/request clock mismatch")
	}
}
func Summary(req *Request, result map[string]any) map[string]any {
	if req.SchemaVersion == 6 {
		days, ok := result["daily_ledger"].([]any)
		if !ok || len(days) == 0 {
			return nil
		}
		d, ok := days[len(days)-1].(map[string]any)
		if !ok {
			return nil
		}
		return map[string]any{"cash": result["final_cash"], "positions_value": d["positions_value"], "equity": result["final_nav"], "commission": result["total_commission"]}
	}
	return map[string]any{"cash": result["cash"], "positions_value": result["positions_value"], "equity": result["equity"], "commission": result["commission"]}
}
