package forge

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type EmbeddedFileSource interface {
	ReadFile(path string) ([]byte, error)
}

const credsBundleVarsPath = "creds/__vars__.json"

type DirLister interface {
	ListDir(path string) ([]string, error)
}

type MapFileSource map[string][]byte

func (m MapFileSource) ReadFile(path string) ([]byte, error) {
	data, ok := m[path]
	if !ok {
		return nil, fmt.Errorf("embedded file %q not found", path)
	}
	return append([]byte(nil), data...), nil
}

func (m MapFileSource) ListDir(dir string) ([]string, error) {
	prefix := strings.TrimSuffix(dir, "/") + "/"
	var paths []string
	for key := range m {
		if strings.HasPrefix(key, prefix) {
			paths = append(paths, key)
		}
	}
	sort.Strings(paths)
	if len(paths) == 0 {
		return nil, fmt.Errorf("directory %q not found or empty", dir)
	}
	return paths, nil
}

type Decryptor interface {
	Decrypt(ciphertext []byte) ([]byte, error)
}

type BundleDecryptor interface {
	DecryptFile(ciphertext []byte, name string) ([]byte, error)
}

type IntegrityVerifier interface {
	Verify(name string, data []byte) error
}

type BuiltLayoutSource struct {
	Root string
}

type FSFileSource struct {
	FS             fs.FS
	CredsEncrypted []byte
}

type StubBundleDecryptor struct{}

type credsBundlePayload struct {
	Recipient string            `json:"recipient"`
	Files     map[string]string `json:"files"`
}

type ExtractConflict struct {
	Path         string
	ExistingSize int64
	ExistingMod  string
	EmbeddedSize int
}

type ExtractConflictError struct {
	Conflicts []ExtractConflict
}

func (e *ExtractConflictError) Error() string {
	var b strings.Builder
	b.WriteString("extract conflict detected:\n\n")
	for _, c := range e.Conflicts {
		fmt.Fprintf(&b, "  CONFLICT  %s\n", c.Path)
		fmt.Fprintf(&b, "            Existing file differs from embedded version.\n")
		fmt.Fprintf(&b, "            Existing: %d bytes, modified %s\n", c.ExistingSize, c.ExistingMod)
		fmt.Fprintf(&b, "            Embedded: %d bytes\n\n", c.EmbeddedSize)
	}
	fmt.Fprintf(&b, "%d conflicts detected. Options:\n", len(e.Conflicts))
	b.WriteString("  --force-extract     Override all conflicting files\n")
	b.WriteString("  --skip-conflicts    Keep existing files, extract only new ones\n")
	return b.String()
}

type ExtractOptions struct {
	Vars           map[string]string
	Source         EmbeddedFileSource
	Decryptor      Decryptor
	Verifier       IntegrityVerifier
	ForceExtract   bool
	SkipConflicts  bool
}

func ExpandDirSources(files []FileSpec, source EmbeddedFileSource) []FileSpec {
	lister, ok := source.(DirLister)
	if !ok {
		return files
	}
	var result []FileSpec
	for _, f := range files {
		key := f.Source
		if f.ContentKey != "" {
			key = f.ContentKey
		}
		subPaths, err := lister.ListDir(key)
		if err != nil || len(subPaths) == 0 {
			if strings.HasSuffix(f.Source, "/") && (err != nil || len(subPaths) == 0) {
				continue
			}
			result = append(result, f)
			continue
		}
		prefix := strings.TrimSuffix(key, "/") + "/"
		for _, sp := range subPaths {
			rel := strings.TrimPrefix(sp, prefix)
			result = append(result, FileSpec{
				Source:     sp,
				Dest:       filepath.Join(f.Dest, rel),
				ContentKey: sp,
				Encrypted:  f.Encrypted,
				Merge:      f.Merge,
				Integrity:  f.Integrity,
				Overwrite:  f.Overwrite,
			})
		}
	}
	return result
}

func Extract(manifest *Manifest, opts ExtractOptions) error {
	type pendingFile struct {
		dest       string
		data       []byte
		contentKey string
	}

	var conflicts []ExtractConflict
	var pending []pendingFile

	files := ExpandDirSources(manifest.Files, opts.Source)
	for _, file := range files {
		contentKey := file.Source
		if file.ContentKey != "" {
			contentKey = file.ContentKey
		}

		dest, err := ExpandTemplate(file.Dest, opts.Vars)
		if err != nil {
			return fmt.Errorf("expand destination %q: %w", file.Dest, err)
		}

		if file.Merge {
			if _, err := os.Stat(dest); err == nil {
				continue
			} else if !os.IsNotExist(err) {
				return fmt.Errorf("stat merge destination %q: %w", dest, err)
			}
		}

		data, err := opts.Source.ReadFile(contentKey)
		if err != nil {
			return fmt.Errorf("read source %q: %w", contentKey, err)
		}

		if file.Encrypted {
			if opts.Decryptor == nil {
				return fmt.Errorf("decryptor is required for encrypted file %q", contentKey)
			}
			if bundleDecryptor, ok := opts.Decryptor.(BundleDecryptor); ok {
				data, err = bundleDecryptor.DecryptFile(data, contentKey)
			} else {
				data, err = opts.Decryptor.Decrypt(data)
			}
			if err != nil {
				return fmt.Errorf("decrypt %q: %w", contentKey, err)
			}
		}

		if file.Overwrite != "always" {
			info, statErr := os.Stat(dest)
			if statErr == nil {
				existing, readErr := os.ReadFile(dest)
				if readErr != nil {
					return fmt.Errorf("read existing %q: %w", dest, readErr)
				}
				if !bytesEqual(existing, data) {
					if opts.ForceExtract {
						// force: write anyway
					} else if opts.SkipConflicts {
						continue
					} else {
						conflicts = append(conflicts, ExtractConflict{
							Path:         dest,
							ExistingSize: info.Size(),
							ExistingMod:  info.ModTime().Format("2006-01-02 15:04"),
							EmbeddedSize: len(data),
						})
						continue
					}
				}
			}
		}

		pending = append(pending, pendingFile{dest: dest, data: data, contentKey: contentKey})
	}

	if len(conflicts) > 0 {
		return &ExtractConflictError{Conflicts: conflicts}
	}

	for _, pf := range pending {
		if err := os.MkdirAll(filepath.Dir(pf.dest), 0o700); err != nil {
			return fmt.Errorf("create parent dir for %q: %w", pf.dest, err)
		}

		if err := os.WriteFile(pf.dest, pf.data, 0o600); err != nil {
			return fmt.Errorf("write %q: %w", pf.dest, err)
		}

		if opts.Verifier != nil {
			if err := opts.Verifier.Verify(pf.contentKey, pf.data); err != nil {
				return fmt.Errorf("verify %q: %w", pf.contentKey, err)
			}
		}
	}

	return nil
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func (s BuiltLayoutSource) ReadFile(path string) ([]byte, error) {
	if strings.HasPrefix(path, "creds/") {
		return os.ReadFile(filepath.Join(s.Root, "creds.age"))
	}
	return os.ReadFile(filepath.Join(s.Root, filepath.FromSlash(path)))
}

func (s FSFileSource) ReadFile(path string) ([]byte, error) {
	if strings.HasPrefix(path, "creds/") {
		return append([]byte(nil), s.CredsEncrypted...), nil
	}

	data, err := fs.ReadFile(s.FS, path)
	if err != nil {
		return nil, err
	}
	return append([]byte(nil), data...), nil
}

func (s FSFileSource) ListDir(dir string) ([]string, error) {
	if s.FS == nil {
		return nil, fmt.Errorf("no filesystem available")
	}
	info, err := fs.Stat(s.FS, dir)
	if err != nil || !info.IsDir() {
		return nil, fmt.Errorf("not a directory: %q", dir)
	}
	var paths []string
	err = fs.WalkDir(s.FS, dir, func(p string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if !d.IsDir() {
			paths = append(paths, p)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(paths) == 0 {
		return nil, fmt.Errorf("directory %q is empty", dir)
	}
	return paths, nil
}

func (d *StubBundleDecryptor) Decrypt(ciphertext []byte) ([]byte, error) {
	return append([]byte(nil), ciphertext...), nil
}

func (d *StubBundleDecryptor) DecryptFile(ciphertext []byte, name string) ([]byte, error) {
	payload, err := parseCredsBundle(ciphertext)
	if err != nil {
		return nil, err
	}
	content, ok := payload.Files[name]
	if !ok {
		return nil, fmt.Errorf("creds bundle missing %q", name)
	}
	return []byte(content), nil
}

func ResolveCredsFromBundle(manifest *Manifest, decryptor Decryptor, ciphertext []byte) (map[string]string, error) {
	if manifest == nil {
		return nil, fmt.Errorf("manifest is nil")
	}
	if len(ciphertext) == 0 {
		return map[string]string{}, nil
	}
	if decryptor == nil {
		return nil, fmt.Errorf("decryptor is required for creds bundle")
	}

	plaintext, err := decryptor.Decrypt(ciphertext)
	if err != nil {
		return nil, fmt.Errorf("decrypt creds bundle: %w", err)
	}

	payload, err := parseCredsBundle(plaintext)
	if err != nil {
		return nil, err
	}

	rawVars, ok := payload.Files[credsBundleVarsPath]
	if !ok || rawVars == "" {
		return map[string]string{}, nil
	}

	var creds map[string]string
	if err := json.Unmarshal([]byte(rawVars), &creds); err != nil {
		return nil, fmt.Errorf("parse creds vars: %w", err)
	}
	if creds == nil {
		return map[string]string{}, nil
	}
	return creds, nil
}

func parseCredsBundle(data []byte) (*credsBundlePayload, error) {
	var payload credsBundlePayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("parse creds bundle: %w", err)
	}
	if payload.Files == nil {
		payload.Files = map[string]string{}
	}
	return &payload, nil
}
