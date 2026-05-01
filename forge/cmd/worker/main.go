package main

import (
	"embed"
	"fmt"
	"io/fs"
	"os"
	"runtime"

	forge "github.com/beastoin/claudecode-telegram/forge"
)

//go:embed embed/manifest.yaml
var manifestYAML []byte

//go:embed all:embed/files embed/checksums.json embed/creds.age
var embeddedFiles embed.FS

//go:embed embed/checksums.json
var checksumsJSON []byte

//go:embed embed/creds.age
var credsEncrypted []byte

func main() {
	filesFS, err := fs.Sub(embeddedFiles, "embed/files")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	opts, err := forge.ParseWorkerCLI(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	switch {
	case opts.EmbeddedPath != "":
		if err := forge.WriteEmbeddedFile(filesFS, opts.EmbeddedPath, os.Stdout); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}

	transport := forge.NewTransport(opts.BridgeURL)

	err = forge.RunEmbeddedWorker(os.Args[1:], forge.WorkerDeps{
		Assets: forge.EmbeddedAssets{
			Manifest:       manifestYAML,
			Files:          filesFS,
			CredsEncrypted: credsEncrypted,
			Checksums:      checksumsJSON,
		},
		Runner:      forge.ShellRunner{},
		Transport:   transport,
		HookManager: forge.HookManager{},
		GOOS:        runtime.GOOS,
		Stdout:      os.Stdout,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
