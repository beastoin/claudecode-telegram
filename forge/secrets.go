package forge

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"filippo.io/age"
)

type AgeCredsEncryptor struct{}
type AgeBundleDecryptor struct {
	Identities []age.Identity
}

func (a AgeCredsEncryptor) Encrypt(bundle map[string][]byte, recipients ...age.Recipient) ([]byte, error) {
	if len(recipients) == 0 {
		return nil, fmt.Errorf("at least one age recipient is required")
	}

	payload := credsBundlePayload{
		Files: make(map[string]string, len(bundle)),
	}
	for name, data := range bundle {
		payload.Files[name] = string(data)
	}

	plaintext, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal creds bundle: %w", err)
	}

	var ciphertext bytes.Buffer
	writer, err := age.Encrypt(&ciphertext, recipients...)
	if err != nil {
		return nil, fmt.Errorf("create age writer: %w", err)
	}
	if _, err := writer.Write(plaintext); err != nil {
		return nil, fmt.Errorf("encrypt creds bundle: %w", err)
	}
	if err := writer.Close(); err != nil {
		return nil, fmt.Errorf("finalize age payload: %w", err)
	}

	return ciphertext.Bytes(), nil
}

func (a AgeBundleDecryptor) Decrypt(data []byte) ([]byte, error) {
	if len(a.Identities) == 0 {
		return nil, fmt.Errorf("at least one age identity is required")
	}

	reader, err := age.Decrypt(bytes.NewReader(data), a.Identities...)
	if err != nil {
		return nil, fmt.Errorf("decrypt creds bundle: %w", err)
	}

	plaintext, err := io.ReadAll(reader)
	if err != nil {
		return nil, fmt.Errorf("read decrypted creds bundle: %w", err)
	}
	return plaintext, nil
}

func (a AgeBundleDecryptor) DecryptFile(data []byte, name string) ([]byte, error) {
	plaintext, err := a.Decrypt(data)
	if err != nil {
		return nil, err
	}

	payload, err := parseCredsBundle(plaintext)
	if err != nil {
		return nil, err
	}

	content, ok := payload.Files[name]
	if !ok {
		return nil, fmt.Errorf("creds bundle missing %q", name)
	}
	return []byte(content), nil
}

func ParseAgeRecipients(spec string) ([]age.Recipient, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return nil, fmt.Errorf("age identity or recipient is required")
	}

	if strings.HasPrefix(spec, "age1") {
		recipient, err := age.ParseX25519Recipient(spec)
		if err != nil {
			return nil, fmt.Errorf("parse age recipient %q: %w", spec, err)
		}
		return []age.Recipient{recipient}, nil
	}

	data, err := os.ReadFile(spec)
	if err != nil {
		return nil, fmt.Errorf("read age identity %q: %w", spec, err)
	}

	identities, identitiesErr := age.ParseIdentities(bytes.NewReader(data))
	if identitiesErr == nil && len(identities) > 0 {
		recipients := make([]age.Recipient, 0, len(identities))
		for _, identity := range identities {
			switch typed := identity.(type) {
			case *age.X25519Identity:
				recipients = append(recipients, typed.Recipient())
			case *age.HybridIdentity:
				recipients = append(recipients, typed.Recipient())
			default:
				return nil, fmt.Errorf("identity %T does not expose a recipient", identity)
			}
		}
		return recipients, nil
	}

	recipients, recipientsErr := age.ParseRecipients(bytes.NewReader(data))
	if recipientsErr == nil && len(recipients) > 0 {
		return recipients, nil
	}

	return nil, fmt.Errorf("parse age recipients from %q: %v", spec, identitiesErr)
}

func ParseAgeIdentities(spec string) ([]age.Identity, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return nil, fmt.Errorf("age identity is required")
	}

	if strings.HasPrefix(spec, "AGE-SECRET-KEY-") {
		identity, err := age.ParseX25519Identity(spec)
		if err != nil {
			return nil, fmt.Errorf("parse age identity: %w", err)
		}
		return []age.Identity{identity}, nil
	}

	data, err := os.ReadFile(spec)
	if err != nil {
		return nil, fmt.Errorf("read age identity %q: %w", spec, err)
	}

	identities, err := age.ParseIdentities(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("parse age identities from %q: %w", spec, err)
	}
	return identities, nil
}
