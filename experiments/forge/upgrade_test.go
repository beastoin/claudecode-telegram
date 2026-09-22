package forge

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	pb "github.com/beastoin/claudecode-telegram/forge/proto/workerforge"
	"google.golang.org/grpc"
)

func TestUpgrader_InterfaceContract(t *testing.T) {
	t.Parallel()

	var upgrader Upgrader = upgraderContractStub{}

	plan, err := upgrader.CheckAvailable(context.Background(), UpgradeRequest{})
	if err != nil {
		t.Fatalf("CheckAvailable() error = %v", err)
	}
	artifact, err := upgrader.Download(context.Background(), plan)
	if err != nil {
		t.Fatalf("Download() error = %v", err)
	}
	if err := upgrader.VerifyChecksum(plan, artifact); err != nil {
		t.Fatalf("VerifyChecksum() error = %v", err)
	}
	if err := upgrader.AtomicReplace(plan, artifact); err != nil {
		t.Fatalf("AtomicReplace() error = %v", err)
	}
	if err := upgrader.Restart(plan); err != nil {
		t.Fatalf("Restart() error = %v", err)
	}
	if err := upgrader.Rollback(plan); err != nil {
		t.Fatalf("Rollback() error = %v", err)
	}
}

type upgraderContractStub struct{}

func (upgraderContractStub) CheckAvailable(context.Context, UpgradeRequest) (UpgradePlan, error) {
	return UpgradePlan{}, nil
}

func (upgraderContractStub) Download(context.Context, UpgradePlan) ([]byte, error) {
	return []byte("artifact"), nil
}

func (upgraderContractStub) VerifyChecksum(UpgradePlan, []byte) error {
	return nil
}

func (upgraderContractStub) AtomicReplace(UpgradePlan, []byte) error {
	return nil
}

func (upgraderContractStub) Restart(UpgradePlan) error {
	return nil
}

func (upgraderContractStub) Rollback(UpgradePlan) error {
	return nil
}

func TestPrebuiltUpgrade_DownloadsAndVerifies(t *testing.T) {
	t.Parallel()

	artifact := []byte("new-worker-binary")
	sum := sha256.Sum256(artifact)
	checksum := hex.EncodeToString(sum[:])
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/mon" {
			http.NotFound(w, r)
			return
		}
		if _, err := w.Write(artifact); err != nil {
			t.Fatalf("Write() error = %v", err)
		}
	}))
	defer server.Close()

	upgrader := &SelfUpgrader{
		Downloader: HTTPDownloader{
			Client: server.Client(),
		},
	}

	plan := UpgradePlan{
		Mode:     "prebuilt",
		URL:      server.URL + "/mon",
		Checksum: checksum,
	}

	got, err := upgrader.Download(context.Background(), plan)
	if err != nil {
		t.Fatalf("Download() error = %v", err)
	}
	if string(got) != string(artifact) {
		t.Fatalf("Download() = %q, want %q", got, artifact)
	}
	if err := upgrader.VerifyChecksum(plan, got); err != nil {
		t.Fatalf("VerifyChecksum() error = %v", err)
	}
}

func TestAtomicReplace_SwapsAndKeepsBackup(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	current := filepath.Join(dir, "mon")
	original := []byte("old-binary")
	replacement := []byte("new-binary")
	if err := os.WriteFile(current, original, 0o755); err != nil {
		t.Fatalf("WriteFile() error = %v", err)
	}

	upgrader := &SelfUpgrader{}
	plan := UpgradePlan{
		BinaryPath: current,
		BackupPath: current + ".bak",
	}

	if err := upgrader.AtomicReplace(plan, replacement); err != nil {
		t.Fatalf("AtomicReplace() error = %v", err)
	}

	gotCurrent, err := os.ReadFile(current)
	if err != nil {
		t.Fatalf("ReadFile(current) error = %v", err)
	}
	if string(gotCurrent) != string(replacement) {
		t.Fatalf("current binary = %q, want %q", gotCurrent, replacement)
	}

	gotBackup, err := os.ReadFile(plan.BackupPath)
	if err != nil {
		t.Fatalf("ReadFile(backup) error = %v", err)
	}
	if string(gotBackup) != string(original) {
		t.Fatalf("backup binary = %q, want %q", gotBackup, original)
	}
}

func TestRollback_RestoresPreviousBinary(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	current := filepath.Join(dir, "mon")
	backup := current + ".bak"
	broken := []byte("broken-binary")
	previous := []byte("previous-binary")
	if err := os.WriteFile(current, broken, 0o755); err != nil {
		t.Fatalf("WriteFile(current) error = %v", err)
	}
	if err := os.WriteFile(backup, previous, 0o755); err != nil {
		t.Fatalf("WriteFile(backup) error = %v", err)
	}

	upgrader := &SelfUpgrader{}
	plan := UpgradePlan{
		BinaryPath: current,
		BackupPath: backup,
	}

	if err := upgrader.Rollback(plan); err != nil {
		t.Fatalf("Rollback() error = %v", err)
	}

	gotCurrent, err := os.ReadFile(current)
	if err != nil {
		t.Fatalf("ReadFile(current) error = %v", err)
	}
	if string(gotCurrent) != string(previous) {
		t.Fatalf("current binary = %q, want %q", gotCurrent, previous)
	}
	if _, err := os.Stat(backup); !os.IsNotExist(err) {
		t.Fatalf("backup still exists, stat error = %v, want not exist", err)
	}
}

func TestSourceRebuild_UsesTransientKey(t *testing.T) {
	t.Parallel()

	builder := &sourceBuilderSpy{
		artifact: []byte("rebuilt-binary"),
	}
	upgrader := &SelfUpgrader{
		Builder: builder,
	}
	plan := UpgradePlan{
		Mode:        "source",
		IdentityKey: "AGE-SECRET-KEY-1TRANSIENTKEY",
	}

	got, err := upgrader.Download(context.Background(), plan)
	if err != nil {
		t.Fatalf("Download() error = %v", err)
	}
	if string(got) != "rebuilt-binary" {
		t.Fatalf("Download() = %q, want rebuilt-binary", got)
	}
	if builder.calls != 1 {
		t.Fatalf("builder.calls = %d, want 1", builder.calls)
	}
	if string(builder.identityKeyCopy) != plan.IdentityKey {
		t.Fatalf("builder.identityKeyCopy = %q, want %q", builder.identityKeyCopy, plan.IdentityKey)
	}
}

func TestSourceRebuild_KeyNeverTouchesDisk(t *testing.T) {
	t.Parallel()

	workDir := t.TempDir()
	key := "AGE-SECRET-KEY-1NEVERONDISK"
	builder := &sourceBuilderSpy{
		artifact: []byte("rebuilt-binary"),
		workDir:  workDir,
	}
	upgrader := &SelfUpgrader{
		Builder: builder,
	}
	plan := UpgradePlan{
		Mode:        "source",
		IdentityKey: key,
	}

	if _, err := upgrader.Download(context.Background(), plan); err != nil {
		t.Fatalf("Download() error = %v", err)
	}

	if len(builder.identityKeyBuf) == 0 {
		t.Fatal("builder.identityKeyBuf is empty, want transient key bytes")
	}
	for i, b := range builder.identityKeyBuf {
		if b != 0 {
			t.Fatalf("builder.identityKeyBuf[%d] = %d, want 0 after zeroization", i, b)
		}
	}

	err := filepath.WalkDir(workDir, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if strings.Contains(string(data), key) {
			t.Fatalf("found key material on disk in %s", path)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("WalkDir() error = %v", err)
	}
}

func TestUpgrade_TriggeredViaBridgeMessage(t *testing.T) {
	t.Parallel()

	req := UpgradeRequest{
		Mode:       "prebuilt",
		URL:        "https://example.com/mon",
		Checksum:   "abc123",
		BinaryPath: "/tmp/mon",
	}
	payload, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("json.Marshal() error = %v", err)
	}

	transport := &StubTransport{
		Connected: true,
		Received: []BridgeMessage{
			{
				Type:    "upgrade",
				Payload: payload,
			},
		},
	}
	upgrader := &upgradeFlowSpy{}

	if err := HandleNextBridgeMessage(context.Background(), transport, upgrader); err != nil {
		t.Fatalf("HandleNextBridgeMessage() error = %v", err)
	}

	if upgrader.checkReq != req {
		t.Fatalf("upgrader.checkReq = %#v, want %#v", upgrader.checkReq, req)
	}
	if len(upgrader.calls) == 0 || upgrader.calls[0] != "check" {
		t.Fatalf("upgrader.calls = %#v, want first call to be check", upgrader.calls)
	}
}

func TestUpgrade_EndToEnd_PrebuiltPath(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	current := filepath.Join(dir, "mon")
	if err := os.WriteFile(current, []byte("old-binary"), 0o755); err != nil {
		t.Fatalf("WriteFile(current) error = %v", err)
	}

	artifact := []byte("new-binary")
	sum := sha256.Sum256(artifact)
	checksum := hex.EncodeToString(sum[:])
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := w.Write(artifact); err != nil {
			t.Fatalf("Write() error = %v", err)
		}
	}))
	defer server.Close()

	restarter := &restartSpy{}
	upgrader := &SelfUpgrader{
		Downloader: HTTPDownloader{
			Client: server.Client(),
		},
		Restarter: restarter,
	}

	err := ExecuteUpgrade(context.Background(), upgrader, UpgradeRequest{
		Mode:       "prebuilt",
		URL:        server.URL,
		Checksum:   checksum,
		BinaryPath: current,
	})
	if err != nil {
		t.Fatalf("ExecuteUpgrade() error = %v", err)
	}

	gotCurrent, err := os.ReadFile(current)
	if err != nil {
		t.Fatalf("ReadFile(current) error = %v", err)
	}
	if string(gotCurrent) != string(artifact) {
		t.Fatalf("current binary = %q, want %q", gotCurrent, artifact)
	}
	if restarter.calls != 1 {
		t.Fatalf("restarter.calls = %d, want 1", restarter.calls)
	}
	if restarter.path != current {
		t.Fatalf("restarter.path = %q, want %q", restarter.path, current)
	}
}

func TestSelfUpgrader_SourceRebuild(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	current := filepath.Join(dir, "worker")
	original := []byte("#!/bin/sh\necho old\n")
	rebuilt := []byte("#!/bin/sh\nif [ \"$1\" = \"--show-embedded\" ]; then echo rebuilt; exit 0; fi\nexit 1\n")
	if err := os.WriteFile(current, original, 0o755); err != nil {
		t.Fatalf("WriteFile(current) error = %v", err)
	}

	builder := &sourceBuilderSpy{
		artifact: rebuilt,
	}
	upgrader := &SelfUpgrader{
		SourceBuilder: builder,
		BinaryPath:    current,
	}

	if err := upgrader.RebuildFromSource(context.Background(), "AGE-SECRET-KEY-1SOURCE"); err != nil {
		t.Fatalf("RebuildFromSource() error = %v", err)
	}

	if builder.calls != 1 {
		t.Fatalf("builder.calls = %d, want 1", builder.calls)
	}
	if builder.plan.BinaryPath != current {
		t.Fatalf("builder.plan.BinaryPath = %q, want %q", builder.plan.BinaryPath, current)
	}
	if string(builder.identityKeyCopy) != "AGE-SECRET-KEY-1SOURCE" {
		t.Fatalf("builder.identityKeyCopy = %q, want identity key", builder.identityKeyCopy)
	}

	gotCurrent, err := os.ReadFile(current)
	if err != nil {
		t.Fatalf("ReadFile(current) error = %v", err)
	}
	if string(gotCurrent) != string(rebuilt) {
		t.Fatalf("current binary = %q, want rebuilt binary", gotCurrent)
	}

	gotBackup, err := os.ReadFile(current + ".bak")
	if err != nil {
		t.Fatalf("ReadFile(backup) error = %v", err)
	}
	if string(gotBackup) != string(original) {
		t.Fatalf("backup binary = %q, want original binary", gotBackup)
	}
}

func TestGRPCDownloader_DownloadsBinary(t *testing.T) {
	t.Parallel()

	artifact := []byte("grpc-downloaded-binary")
	srv, addr := startUpgradeGRPCServer(t, artifact)
	defer srv.Stop()

	transport := &GRPCTransport{}
	if err := transport.Connect(context.Background(), addr); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	defer transport.Close()

	downloader := GRPCDownloader{Transport: transport, Name: "mon"}
	got, err := downloader.Download(context.Background(), "https://bridge/v1.2.3")
	if err != nil {
		t.Fatalf("Download() error = %v", err)
	}
	if string(got) != string(artifact) {
		t.Fatalf("Download() = %q, want %q", got, artifact)
	}
}

func TestGRPCDownloader_FailsWhenDisconnected(t *testing.T) {
	t.Parallel()

	downloader := GRPCDownloader{Transport: &GRPCTransport{}, Name: "mon"}
	_, err := downloader.Download(context.Background(), "v1.0.0")
	if err == nil {
		t.Fatal("Download() on disconnected transport should fail")
	}
}

func TestGoSourceBuilder_Rebuild(t *testing.T) {
	t.Parallel()

	tmpDir := t.TempDir()
	mainGo := `package main
import "fmt"
func main() { fmt.Println("rebuilt") }
`
	cmdDir := filepath.Join(tmpDir, "cmd", "worker")
	if err := os.MkdirAll(cmdDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cmdDir, "main.go"), []byte(mainGo), 0o644); err != nil {
		t.Fatal(err)
	}
	goMod := "module test/rebuild\n\ngo 1.25.0\n"
	if err := os.WriteFile(filepath.Join(tmpDir, "go.mod"), []byte(goMod), 0o644); err != nil {
		t.Fatal(err)
	}

	builder := GoSourceBuilder{ModuleDir: tmpDir}
	got, err := builder.Rebuild(context.Background(), SourceBuildRequest{
		Plan: UpgradePlan{Mode: "source"},
	})
	if err != nil {
		t.Fatalf("Rebuild() error = %v", err)
	}
	if len(got) == 0 {
		t.Fatal("Rebuild() returned empty binary")
	}
}

type upgradeBridgeServer struct {
	pb.UnimplementedBridgeServer
	artifact []byte
}

func (s *upgradeBridgeServer) Check(context.Context, *pb.HealthCheckRequest) (*pb.HealthCheckResponse, error) {
	return &pb.HealthCheckResponse{Status: pb.HealthCheckResponse_SERVING}, nil
}

func (s *upgradeBridgeServer) CheckUpgrade(_ context.Context, req *pb.UpgradeCheckRequest) (*pb.UpgradeCheckResponse, error) {
	sum := sha256.Sum256(s.artifact)
	return &pb.UpgradeCheckResponse{
		Available: true,
		Version:   "1.2.3",
		Checksum:  hex.EncodeToString(sum[:]),
		Size:      int64(len(s.artifact)),
	}, nil
}

func (s *upgradeBridgeServer) DownloadBinary(req *pb.DownloadRequest, stream grpc.ServerStreamingServer[pb.BinaryChunk]) error {
	return stream.Send(&pb.BinaryChunk{
		Data:  s.artifact,
		Final: true,
	})
}

func startUpgradeGRPCServer(t *testing.T, artifact []byte) (*grpc.Server, string) {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("failed to listen: %v", err)
	}
	srv := grpc.NewServer()
	pb.RegisterBridgeServer(srv, &upgradeBridgeServer{artifact: artifact})
	go srv.Serve(lis)
	t.Cleanup(func() { srv.Stop() })
	return srv, lis.Addr().String()
}

type sourceBuilderSpy struct {
	artifact        []byte
	calls           int
	workDir         string
	plan            UpgradePlan
	identityKeyBuf  []byte
	identityKeyCopy []byte
}

func (s *sourceBuilderSpy) Rebuild(_ context.Context, req SourceBuildRequest) ([]byte, error) {
	s.calls++
	s.plan = req.Plan
	s.identityKeyBuf = req.IdentityKey
	s.identityKeyCopy = append([]byte(nil), req.IdentityKey...)
	if s.workDir != "" {
		if err := os.WriteFile(filepath.Join(s.workDir, "build.log"), []byte("rebuilt"), 0o600); err != nil {
			return nil, err
		}
	}
	return s.artifact, nil
}

type upgradeFlowSpy struct {
	calls    []string
	checkReq UpgradeRequest
}

func (u *upgradeFlowSpy) CheckAvailable(_ context.Context, req UpgradeRequest) (UpgradePlan, error) {
	u.calls = append(u.calls, "check")
	u.checkReq = req
	return UpgradePlan{
		Mode:       req.Mode,
		URL:        req.URL,
		Checksum:   req.Checksum,
		BinaryPath: req.BinaryPath,
		BackupPath: req.BinaryPath + ".bak",
	}, nil
}

func (u *upgradeFlowSpy) Download(context.Context, UpgradePlan) ([]byte, error) {
	u.calls = append(u.calls, "download")
	return []byte("artifact"), nil
}

func (u *upgradeFlowSpy) VerifyChecksum(UpgradePlan, []byte) error {
	u.calls = append(u.calls, "verify")
	return nil
}

func (u *upgradeFlowSpy) AtomicReplace(UpgradePlan, []byte) error {
	u.calls = append(u.calls, "replace")
	return nil
}

func (u *upgradeFlowSpy) Restart(UpgradePlan) error {
	u.calls = append(u.calls, "restart")
	return nil
}

func (u *upgradeFlowSpy) Rollback(UpgradePlan) error {
	u.calls = append(u.calls, "rollback")
	return nil
}

type restartSpy struct {
	calls int
	path  string
}

func (r *restartSpy) Restart(path string) error {
	r.calls++
	r.path = path
	return nil
}
