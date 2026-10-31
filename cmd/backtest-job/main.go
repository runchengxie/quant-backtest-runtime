package main

import (
	"github.com/runchengxie/quant-backtest-runtime/internal/app"
	"github.com/runchengxie/quant-backtest-runtime/internal/supervisor"
	"os"
)

func main() {
	if supervisor.HandleExec(os.Args[1:]) {
		return
	}
	os.Exit(app.Run(os.Args[1:], os.Stdout, os.Stderr))
}
