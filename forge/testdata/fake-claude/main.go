// fake-claude emulates Claude Code's auth TUI screens as a deterministic state
// machine. It reads scenario definitions from a JSON file (passed via -scenario)
// and transitions through states based on stdin input.
//
// Build: go build -o fake-claude ./forge/testdata/fake-claude
// Usage: fake-claude -scenario happy_path.json
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

type Reaction struct {
	OnInput string   `json:"on_input"`
	Output  []string `json:"output"`
}

type Scenario struct {
	Name          string     `json:"name"`
	InitialOutput []string   `json:"initial_output"`
	Reactions     []Reaction `json:"reactions"`
	SuccessMarker string     `json:"success_marker"`
	RejectBracketedPaste bool `json:"reject_bracketed_paste"`
}

func main() {
	scenarioPath := ""
	for i, arg := range os.Args[1:] {
		if arg == "-scenario" && i+1 < len(os.Args)-1 {
			scenarioPath = os.Args[i+2]
		}
	}

	if scenarioPath == "" {
		fmt.Fprintf(os.Stderr, "usage: fake-claude -scenario <path.json>\n")
		os.Exit(1)
	}

	data, err := os.ReadFile(scenarioPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "read scenario: %v\n", err)
		os.Exit(1)
	}

	var scenario Scenario
	if err := json.Unmarshal(data, &scenario); err != nil {
		fmt.Fprintf(os.Stderr, "parse scenario: %v\n", err)
		os.Exit(1)
	}

	for _, line := range scenario.InitialOutput {
		fmt.Println(line)
	}

	scanner := bufio.NewScanner(os.Stdin)
	reactionIndex := 0

	for scanner.Scan() {
		input := scanner.Text()

		if scenario.RejectBracketedPaste && strings.Contains(input, "\x1b[200~") {
			continue
		}

		if reactionIndex < len(scenario.Reactions) {
			reaction := scenario.Reactions[reactionIndex]
			if reaction.OnInput == "" || reaction.OnInput == input || strings.Contains(input, reaction.OnInput) {
				time.Sleep(100 * time.Millisecond)
				for _, line := range reaction.Output {
					fmt.Println(line)
				}
				reactionIndex++
			}
		}
	}

	if scenario.SuccessMarker != "" && reactionIndex >= len(scenario.Reactions) {
		time.Sleep(200 * time.Millisecond)
		fmt.Println(scenario.SuccessMarker)
	}

	time.Sleep(2 * time.Second)
}
