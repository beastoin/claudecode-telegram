package forge

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"
	"time"
)

func TestWebConnector_AuthIntegration(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	tmpDir := t.TempDir()
	session := fmt.Sprintf("web-auth-%d", os.Getpid())

	fakeScript := filepath.Join(tmpDir, "fake-claude-web.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Visit https://claude.ai/oauth/web-integration-test to authenticate"
read -t 30 auth_code
if [ -n "$auth_code" ]; then
  sleep 0.3
  echo "What can I help you with?"
fi
sleep 2
`), 0755)

	runner := ShellRunner{}
	runtime := &TmuxRuntime{Runner: runner, Session: session}
	runtime.SetLaunchCommand(fakeScript)
	if err := runtime.Start(); err != nil {
		t.Fatalf("start tmux: %v", err)
	}
	t.Cleanup(func() { exec.Command("tmux", "kill-session", "-t", session).Run() })

	time.Sleep(500 * time.Millisecond)

	webConn := &WebConnector{}
	webConn.Init(context.Background(), ConnectorConfig{
		WorkerName: "web-auth-test",
		Config:     map[string]string{},
	})
	defer webConn.Close()

	spec := testSpec()
	spec.URLTimeout = 15 * time.Second
	spec.CodeTimeout = 15 * time.Second

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "web-auth-test",
	}

	errCh := make(chan error, 1)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	go func() { errCh <- coord.Run(ctx, AdaptConnectorForAuth(webConn)) }()

	url := fmt.Sprintf("http://127.0.0.1:%d", webConn.Port())
	var authMsgID string
	deadline := time.After(15 * time.Second)
	for authMsgID == "" {
		authMsgID = webConn.PendingAuthPromptID()
		if authMsgID != "" {
			break
		}
		select {
		case <-deadline:
			t.Fatal("auth prompt never appeared")
		case <-time.After(100 * time.Millisecond):
		}
	}

	codeBody := fmt.Sprintf(`{"text":"web-auth-code","from":"web","reply_to_id":"%s"}`, authMsgID)
	resp, _ := http.Post(url+"/api/send", "application/json", bytes.NewBufferString(codeBody))
	resp.Body.Close()

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("Run() error = %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("auth did not complete")
	}

	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}
}

func TestWebConnector_E2E(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	tmpDir := t.TempDir()
	webPort := findFreePort(t)

	fakeScript := filepath.Join(tmpDir, "fake-claude-web-e2e.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Visit https://claude.ai/oauth/web-e2e to authenticate"
read -t 60 auth_code
if [ -n "$auth_code" ]; then
  sleep 0.3
  echo "What can I help you with?"
fi
sleep 3
`), 0755)

	manifest := fmt.Sprintf(`
name: web-e2e-test
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("web-e2e-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"web-e2e-test").Run()
	})

	engine := &fakeAuthEngine{launchCmd: fakeScript}

	var stdout bytes.Buffer
	errCh := make(chan error, 1)
	go func() {
		errCh <- RunEmbeddedWorker(
			[]string{"auth",
				"--connector", "web",
				"--connector-opt", fmt.Sprintf("WEB_PORT=%d", webPort),
				"--session-prefix", sessionPrefix,
			},
			WorkerDeps{
				Assets: EmbeddedAssets{
					Manifest: []byte(manifest),
					Files:    fstest.MapFS{},
				},
				Engine: engine,
				GOOS:   "linux",
				Stdout: &stdout,
			},
		)
	}()

	url := fmt.Sprintf("http://127.0.0.1:%d", webPort)
	if !waitForURL(t, url+"/api/health", 15*time.Second) {
		t.Fatal("web connector never became ready")
	}

	resp, _ := http.Get(url + "/")
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !strings.Contains(string(body), "web-e2e-test") {
		t.Fatal("chat page should contain worker name")
	}

	sseCtx, sseCancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer sseCancel()
	sseReq, _ := http.NewRequestWithContext(sseCtx, "GET", url+"/events", nil)
	sseResp, err := http.DefaultClient.Do(sseReq)
	if err != nil {
		t.Fatalf("SSE connect error = %v", err)
	}
	defer sseResp.Body.Close()

	buf := make([]byte, 8192)
	var authMsgID string
	for authMsgID == "" {
		n, err := sseResp.Body.Read(buf)
		if err != nil {
			t.Fatalf("SSE read error = %v", err)
		}
		data := string(buf[:n])
		if strings.Contains(data, "msg auth") {
			if idx := strings.Index(data, `value="`); idx >= 0 {
				rest := data[idx+7:]
				if end := strings.Index(rest, `"`); end >= 0 {
					authMsgID = rest[:end]
				}
			}
		}
	}

	codeBody := fmt.Sprintf(`{"text":"web-e2e-code","from":"web","reply_to_id":"%s"}`, authMsgID)
	resp, _ = http.Post(url+"/api/send", "application/json", bytes.NewBufferString(codeBody))
	resp.Body.Close()

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("RunEmbeddedWorker() error = %v", err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("RunEmbeddedWorker did not return")
	}
}
