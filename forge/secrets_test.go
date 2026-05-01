package forge

import (
	"bytes"
	"strings"
	"testing"

	"filippo.io/age"
)

func TestAgeCredsEncryptor_EncryptsBundleWithRealAgeRecipient(t *testing.T) {
	t.Parallel()

	identity, err := age.GenerateX25519Identity()
	if err != nil {
		t.Fatalf("age.GenerateX25519Identity() error = %v", err)
	}

	encryptor := AgeCredsEncryptor{}
	ciphertext, err := encryptor.Encrypt(map[string][]byte{
		"test.env": []byte("SECRET=value"),
	}, identity.Recipient())
	if err != nil {
		t.Fatalf("Encrypt() error = %v", err)
	}

	if bytes.Contains(ciphertext, []byte("SECRET=value")) {
		t.Fatalf("ciphertext = %q, want non-plaintext output", ciphertext)
	}
	if !strings.HasPrefix(string(ciphertext), "age-encryption.org") {
		t.Fatalf("ciphertext = %q, want age header", ciphertext)
	}
}

func TestAgeBundleDecryptor_DecryptsSpecificCredFileFromBundle(t *testing.T) {
	t.Parallel()

	identity, err := age.GenerateX25519Identity()
	if err != nil {
		t.Fatalf("age.GenerateX25519Identity() error = %v", err)
	}

	encryptor := AgeCredsEncryptor{}
	ciphertext, err := encryptor.Encrypt(map[string][]byte{
		"test.env": []byte("SECRET=value"),
	}, identity.Recipient())
	if err != nil {
		t.Fatalf("Encrypt() error = %v", err)
	}

	decryptor := AgeBundleDecryptor{Identities: []age.Identity{identity}}

	plaintext, err := decryptor.Decrypt(ciphertext)
	if err != nil {
		t.Fatalf("Decrypt() error = %v", err)
	}
	if !strings.Contains(string(plaintext), `"test.env":"SECRET=value"`) {
		t.Fatalf("plaintext = %q, want bundled file contents", plaintext)
	}

	fileData, err := decryptor.DecryptFile(ciphertext, "test.env")
	if err != nil {
		t.Fatalf("DecryptFile() error = %v", err)
	}
	if string(fileData) != "SECRET=value" {
		t.Fatalf("DecryptFile() = %q, want %q", fileData, "SECRET=value")
	}
}

func TestResolveCredsFromBundle_DecryptsCredVarsFromRealAgeBundle(t *testing.T) {
	t.Parallel()

	identity, err := age.GenerateX25519Identity()
	if err != nil {
		t.Fatalf("age.GenerateX25519Identity() error = %v", err)
	}

	encryptor := AgeCredsEncryptor{}
	ciphertext, err := encryptor.Encrypt(map[string][]byte{
		credsBundleVarsPath: []byte(`{"ANTHROPIC_API_KEY":"sk-test-123"}`),
	}, identity.Recipient())
	if err != nil {
		t.Fatalf("Encrypt() error = %v", err)
	}

	creds, err := ResolveCredsFromBundle(&Manifest{}, AgeBundleDecryptor{
		Identities: []age.Identity{identity},
	}, ciphertext)
	if err != nil {
		t.Fatalf("ResolveCredsFromBundle() error = %v", err)
	}

	if got := creds["ANTHROPIC_API_KEY"]; got != "sk-test-123" {
		t.Fatalf("creds[ANTHROPIC_API_KEY] = %q, want %q", got, "sk-test-123")
	}
}
