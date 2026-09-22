package forge

import (
	"strings"
	"testing"
)

func FuzzParseWorkerCLI(f *testing.F) {
	f.Add("check")
	f.Add("check --bridge-url http://localhost:8271")
	f.Add("run --connector local")
	f.Add("describe check")
	f.Add("version")
	f.Add("verify --output-json")
	f.Add("")

	f.Fuzz(func(t *testing.T, argsStr string) {
		args := strings.Fields(argsStr)
		ParseWorkerCLI(args)
	})
}
