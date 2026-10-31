package app

func queueCapability() map[string]any {
	return map[string]any{
		"id": "serve", "description": "Run the persistent single-machine queue with a bounded worker count",
		"owner": "quant-backtest-runtime", "contract_version": 1,
		"side_effects":  []string{"scheduler_lock", "recovery", "worker_execution", "result_publication"},
		"permission":    "submit",
		"input_schema":  map[string]any{"type": "object", "properties": map[string]any{"concurrency": map[string]any{"type": "integer", "minimum": 1, "maximum": 256, "default": 1}, "once": map[string]any{"type": "boolean", "default": false}}, "required": []string{}, "additionalProperties": false},
		"output_schema": map[string]any{"type": "object"}, "output_contract": "object",
		"invocation":    "backtest-job [--database DB --artifact-root INPUTS --result-root RESULTS] serve --concurrency N",
		"preconditions": []string{"explicit storage paths", "authorized local account", "one scheduler per database"},
		"retry":         "inspect_state_first",
		"examples":      []string{"backtest-job --database /data/runtime/jobs.sqlite --artifact-root /data/runtime/artifacts --result-root /data/runtime/results serve --concurrency 4"},
		"limitations":   []string{"Use queued submissions only", "Discovery grants no permission", "Child memory limits exclude Go orchestration"},
	}
}
