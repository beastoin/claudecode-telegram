// omi — Checksum-verified Omi infrastructure CLI.
//
// Single binary, single SHA256 checksum in Bitwarden.
// All subcommands live in this file. Add new ones by adding a case to main()
// and a func below.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Usage: omi <subcommand> [args...]")
		fmt.Fprintln(os.Stderr, "Subcommands: bucket-versioning-set")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "bucket-versioning-set":
		os.Exit(bucketVersioningSet(os.Args[2:]))
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", os.Args[1])
		os.Exit(2)
	}
}

// ── bucket-versioning-set ──────────────────────────────────────────

type gcsBucket struct {
	StorageURL string `json:"storage_url"`
	Metadata   struct {
		Name       string `json:"name"`
		Versioning struct {
			Enabled bool `json:"enabled"`
		} `json:"versioning"`
	} `json:"metadata"`
}

func bucketVersioningSet(args []string) int {
	project := "based-hardware"
	dryRun := false

	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--project":
			if i+1 >= len(args) {
				fmt.Fprintln(os.Stderr, "--project requires a value")
				return 2
			}
			i++
			project = args[i]
		case "--dry-run":
			dryRun = true
		default:
			fmt.Fprintf(os.Stderr, "unknown flag: %s\n", args[i])
			return 2
		}
	}

	out, err := exec.Command("gcloud", "storage", "buckets", "list",
		"--project", project, "--format=json").Output()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error listing buckets: %v\n", err)
		return 1
	}

	var buckets []gcsBucket
	if err := json.Unmarshal(out, &buckets); err != nil {
		fmt.Fprintf(os.Stderr, "Error parsing bucket list: %v\n", err)
		return 1
	}

	if len(buckets) == 0 {
		fmt.Println("No buckets found.")
		return 0
	}

	changed := 0
	for _, b := range buckets {
		display := strings.TrimRight(b.StorageURL, "/")
		if display == "" {
			display = "gs://" + b.Metadata.Name
		}

		if b.Metadata.Versioning.Enabled {
			fmt.Printf("OK %s (versioning already enabled)\n", display)
			continue
		}

		if dryRun {
			fmt.Printf("Would enable versioning on %s\n", display)
			changed++
			continue
		}

		cmd := exec.Command("gcloud", "storage", "buckets", "update",
			strings.TrimRight(b.StorageURL, "/"), "--versioning")
		if out, err := cmd.CombinedOutput(); err != nil {
			fmt.Fprintf(os.Stderr, "FAILED %s: %s\n", display, strings.TrimSpace(string(out)))
		} else {
			fmt.Printf("Enabled versioning on %s\n", display)
			changed++
		}
	}

	action := "changed"
	if dryRun {
		action = "would change"
	}
	fmt.Printf("Total: %d buckets, %d %s\n", len(buckets), changed, action)
	return 0
}
