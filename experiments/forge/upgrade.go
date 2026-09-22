package forge

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	pb "github.com/beastoin/claudecode-telegram/forge/proto/workerforge"
)

type Upgrader interface {
	CheckAvailable(ctx context.Context, req UpgradeRequest) (UpgradePlan, error)
	Download(ctx context.Context, plan UpgradePlan) ([]byte, error)
	VerifyChecksum(plan UpgradePlan, artifact []byte) error
	AtomicReplace(plan UpgradePlan, artifact []byte) error
	Restart(plan UpgradePlan) error
	Rollback(plan UpgradePlan) error
}

type UpgradeRequest struct {
	Mode        string
	URL         string
	Checksum    string
	BinaryPath  string
	IdentityKey string
}

type UpgradePlan struct {
	Mode        string
	URL         string
	Checksum    string
	BinaryPath  string
	BackupPath  string
	IdentityKey string
}

type ArtifactDownloader interface {
	Download(ctx context.Context, url string) ([]byte, error)
}

type HTTPDownloader struct {
	Client *http.Client
}

func (d HTTPDownloader) Download(ctx context.Context, url string) ([]byte, error) {
	client := d.Client
	if client == nil {
		client = http.DefaultClient
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("build download request: %w", err)
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("download artifact: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download artifact: unexpected status %s", resp.Status)
	}

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read artifact body: %w", err)
	}

	return data, nil
}

type GRPCDownloader struct {
	Transport *GRPCTransport
	Name      string
}

func (d GRPCDownloader) Download(ctx context.Context, url string) ([]byte, error) {
	if d.Transport == nil {
		return nil, fmt.Errorf("grpc transport is required")
	}
	client := d.Transport.Client()
	if client == nil {
		return nil, fmt.Errorf("grpc transport is not connected")
	}

	version := url
	if idx := strings.LastIndex(url, "/"); idx >= 0 {
		version = url[idx+1:]
	}

	target := fmt.Sprintf("%s/%s", runtime.GOOS, runtime.GOARCH)
	stream, err := client.DownloadBinary(ctx, &pb.DownloadRequest{
		Name:    d.Name,
		Version: version,
		Target:  target,
	})
	if err != nil {
		return nil, fmt.Errorf("grpc download binary: %w", err)
	}

	var buf []byte
	for {
		chunk, err := stream.Recv()
		if err != nil {
			return nil, fmt.Errorf("grpc download recv: %w", err)
		}
		buf = append(buf, chunk.Data...)
		if chunk.Final {
			break
		}
	}
	return buf, nil
}

type SourceBuildRequest struct {
	Plan        UpgradePlan
	IdentityKey []byte
}

type SourceBuilder interface {
	Rebuild(ctx context.Context, req SourceBuildRequest) ([]byte, error)
}

type GoSourceBuilder struct {
	ModuleDir string
}

func (b GoSourceBuilder) Rebuild(ctx context.Context, req SourceBuildRequest) ([]byte, error) {
	moduleDir := b.ModuleDir
	if moduleDir == "" {
		var err error
		moduleDir, err = currentModuleRoot()
		if err != nil {
			return nil, fmt.Errorf("resolve module root: %w", err)
		}
	}

	tmpDir, err := os.MkdirTemp("", "forge-rebuild-*")
	if err != nil {
		return nil, fmt.Errorf("create temp dir: %w", err)
	}
	defer os.RemoveAll(tmpDir)

	outPath := filepath.Join(tmpDir, "worker")
	cmd := exec.CommandContext(ctx, "go", "build", "-o", outPath, "./cmd/worker")
	cmd.Dir = moduleDir
	cmd.Env = append(os.Environ(),
		"GOOS="+runtime.GOOS,
		"GOARCH="+runtime.GOARCH,
	)

	output, err := cmd.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("go build: %w\n%s", err, output)
	}

	data, err := os.ReadFile(outPath)
	if err != nil {
		return nil, fmt.Errorf("read built binary: %w", err)
	}
	return data, nil
}

type Restarter interface {
	Restart(path string) error
}

type SelfUpgrader struct {
	Downloader    ArtifactDownloader
	SourceBuilder SourceBuilder
	Builder       SourceBuilder
	Restarter     Restarter
	BinaryPath    string
}

func (u *SelfUpgrader) CheckAvailable(_ context.Context, req UpgradeRequest) (UpgradePlan, error) {
	plan := UpgradePlan{
		Mode:        req.Mode,
		URL:         req.URL,
		Checksum:    req.Checksum,
		BinaryPath:  req.BinaryPath,
		IdentityKey: req.IdentityKey,
	}
	if plan.BinaryPath != "" {
		plan.BackupPath = plan.BinaryPath + ".bak"
	}
	return plan, nil
}

func (u *SelfUpgrader) Download(ctx context.Context, plan UpgradePlan) ([]byte, error) {
	if strings.EqualFold(plan.Mode, "source") {
		builder := u.sourceBuilder()
		if builder == nil {
			return nil, fmt.Errorf("source builder is required")
		}
		if strings.TrimSpace(plan.IdentityKey) == "" {
			return nil, fmt.Errorf("identity key is required")
		}

		identityKey := []byte(plan.IdentityKey)
		defer zeroBytes(identityKey)

		return builder.Rebuild(ctx, SourceBuildRequest{
			Plan:        plan,
			IdentityKey: identityKey,
		})
	}

	if u.Downloader == nil {
		return nil, fmt.Errorf("downloader is required")
	}
	if plan.URL == "" {
		return nil, fmt.Errorf("upgrade url is required")
	}
	return u.Downloader.Download(ctx, plan.URL)
}

func (u *SelfUpgrader) RebuildFromSource(ctx context.Context, identityKey string) error {
	builder := u.sourceBuilder()
	if builder == nil {
		return fmt.Errorf("source builder is required")
	}
	if strings.TrimSpace(identityKey) == "" {
		return fmt.Errorf("identity key is required")
	}

	binaryPath, err := u.selfBinaryPath()
	if err != nil {
		return err
	}

	plan := UpgradePlan{
		Mode:        "source",
		BinaryPath:  binaryPath,
		BackupPath:  binaryPath + ".bak",
		IdentityKey: identityKey,
	}

	identityKeyBytes := []byte(identityKey)
	defer zeroBytes(identityKeyBytes)

	artifact, err := builder.Rebuild(ctx, SourceBuildRequest{
		Plan:        plan,
		IdentityKey: identityKeyBytes,
	})
	if err != nil {
		return err
	}

	if err := u.AtomicReplace(plan, artifact); err != nil {
		return u.rollbackAfterFailure("atomic replace", err, plan)
	}
	if err := verifyBinaryRuns(ctx, plan.BinaryPath); err != nil {
		return u.rollbackAfterFailure("verify rebuilt binary", err, plan)
	}

	return nil
}

func (u *SelfUpgrader) sourceBuilder() SourceBuilder {
	if u.SourceBuilder != nil {
		return u.SourceBuilder
	}
	return u.Builder
}

func (u *SelfUpgrader) selfBinaryPath() (string, error) {
	if u.BinaryPath != "" {
		return u.BinaryPath, nil
	}

	path, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("resolve current executable: %w", err)
	}
	return path, nil
}

func (u *SelfUpgrader) rollbackAfterFailure(operation string, err error, plan UpgradePlan) error {
	if rollbackErr := u.Rollback(plan); rollbackErr != nil {
		return fmt.Errorf("%s: %v; rollback: %w", operation, err, rollbackErr)
	}
	return err
}

func verifyBinaryRuns(ctx context.Context, binaryPath string) error {
	verifyCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	cmd := exec.CommandContext(verifyCtx, binaryPath, "--show-embedded", "placeholder.txt")
	output, err := cmd.CombinedOutput()
	if verifyCtx.Err() != nil {
		return fmt.Errorf("run rebuilt binary: %w", verifyCtx.Err())
	}
	if err != nil {
		return fmt.Errorf("run rebuilt binary: %w\n%s", err, output)
	}

	return nil
}

func (u *SelfUpgrader) VerifyChecksum(plan UpgradePlan, artifact []byte) error {
	if strings.TrimSpace(plan.Checksum) == "" {
		return nil
	}

	sum := sha256.Sum256(artifact)
	got := hex.EncodeToString(sum[:])
	if !strings.EqualFold(got, strings.TrimSpace(plan.Checksum)) {
		return fmt.Errorf("checksum mismatch: got %s want %s", got, plan.Checksum)
	}

	return nil
}

func (u *SelfUpgrader) AtomicReplace(plan UpgradePlan, artifact []byte) error {
	if plan.BinaryPath == "" {
		return fmt.Errorf("binary path is required")
	}

	info, err := os.Stat(plan.BinaryPath)
	if err != nil {
		return fmt.Errorf("stat current binary: %w", err)
	}

	backupPath := plan.BackupPath
	if backupPath == "" {
		backupPath = plan.BinaryPath + ".bak"
	}

	if err := os.RemoveAll(backupPath); err != nil {
		return fmt.Errorf("remove existing backup: %w", err)
	}
	if err := os.Rename(plan.BinaryPath, backupPath); err != nil {
		return fmt.Errorf("backup current binary: %w", err)
	}

	dir := filepath.Dir(plan.BinaryPath)
	tmp, err := os.CreateTemp(dir, filepath.Base(plan.BinaryPath)+".tmp-*")
	if err != nil {
		return fmt.Errorf("create replacement temp file: %w", err)
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)

	if err := tmp.Chmod(info.Mode()); err != nil {
		tmp.Close()
		return fmt.Errorf("chmod replacement temp file: %w", err)
	}
	if _, err := tmp.Write(artifact); err != nil {
		tmp.Close()
		return fmt.Errorf("write replacement artifact: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close replacement temp file: %w", err)
	}

	if err := os.Rename(tmpPath, plan.BinaryPath); err != nil {
		return fmt.Errorf("activate replacement binary: %w", err)
	}

	return nil
}

func (u *SelfUpgrader) Restart(plan UpgradePlan) error {
	if u.Restarter == nil {
		return nil
	}
	if plan.BinaryPath == "" {
		return fmt.Errorf("binary path is required")
	}
	return u.Restarter.Restart(plan.BinaryPath)
}

func (u *SelfUpgrader) Rollback(plan UpgradePlan) error {
	if plan.BinaryPath == "" {
		return fmt.Errorf("binary path is required")
	}

	backupPath := plan.BackupPath
	if backupPath == "" {
		backupPath = plan.BinaryPath + ".bak"
	}

	if _, err := os.Stat(backupPath); err != nil {
		return fmt.Errorf("stat backup binary: %w", err)
	}

	if err := os.Remove(plan.BinaryPath); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove failed binary: %w", err)
	}
	if err := os.Rename(backupPath, plan.BinaryPath); err != nil {
		return fmt.Errorf("restore backup binary: %w", err)
	}

	return nil
}

func currentModuleRoot() (string, error) {
	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		return "", fmt.Errorf("resolve module root: runtime caller unavailable")
	}
	return filepath.Dir(filename), nil
}

func zeroBytes(data []byte) {
	for i := range data {
		data[i] = 0
	}
}

func HandleNextBridgeMessage(ctx context.Context, transport Transport, upgrader Upgrader) error {
	msg, err := transport.ReceiveMessage(ctx)
	if err != nil {
		return err
	}

	return HandleBridgeMessage(ctx, msg, upgrader)
}

func HandleBridgeMessage(ctx context.Context, msg BridgeMessage, upgrader Upgrader) error {
	if !strings.EqualFold(msg.Type, "upgrade") {
		return nil
	}

	req, err := ParseUpgradeRequest(msg)
	if err != nil {
		return err
	}

	return ExecuteUpgrade(ctx, upgrader, req)
}

func ParseUpgradeRequest(msg BridgeMessage) (UpgradeRequest, error) {
	var req UpgradeRequest
	if len(msg.Payload) == 0 {
		return req, fmt.Errorf("upgrade payload is required")
	}
	if err := json.Unmarshal(msg.Payload, &req); err != nil {
		return req, fmt.Errorf("parse upgrade payload: %w", err)
	}
	return req, nil
}

func ExecuteUpgrade(ctx context.Context, upgrader Upgrader, req UpgradeRequest) error {
	plan, err := upgrader.CheckAvailable(ctx, req)
	if err != nil {
		return err
	}

	artifact, err := upgrader.Download(ctx, plan)
	if err != nil {
		return err
	}
	if err := upgrader.VerifyChecksum(plan, artifact); err != nil {
		return err
	}
	if err := upgrader.AtomicReplace(plan, artifact); err != nil {
		if rollbackErr := upgrader.Rollback(plan); rollbackErr != nil {
			return fmt.Errorf("atomic replace: %v; rollback: %w", err, rollbackErr)
		}
		return err
	}
	if err := upgrader.Restart(plan); err != nil {
		if rollbackErr := upgrader.Rollback(plan); rollbackErr != nil {
			return fmt.Errorf("restart: %v; rollback: %w", err, rollbackErr)
		}
		return err
	}

	return nil
}
