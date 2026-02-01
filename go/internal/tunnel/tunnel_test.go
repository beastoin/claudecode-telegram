package tunnel

import "testing"

func TestExtractTunnelURL(t *testing.T) {
	tests := []struct {
		name string
		line string
		want string
	}{
		{
			name: "trycloudflare url",
			line: "INF + Your quick Tunnel has been created! Visit it at: https://abc-123.trycloudflare.com",
			want: "https://abc-123.trycloudflare.com",
		},
		{
			name: "cfargotunnel url",
			line: "route: https://example.cfargotunnel.com established",
			want: "https://example.cfargotunnel.com",
		},
		{
			name: "no tunnel url",
			line: "Starting tunnel for http://localhost:8080",
			want: "",
		},
		{
			name: "trailing punctuation",
			line: "URL: https://demo.trycloudflare.com.",
			want: "https://demo.trycloudflare.com",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := ExtractTunnelURL(tt.line)
			if tt.want == "" {
				if ok {
					t.Fatalf("expected no match, got %q", got)
				}
				return
			}
			if !ok {
				t.Fatalf("expected match %q, got none", tt.want)
			}
			if got != tt.want {
				t.Fatalf("expected %q, got %q", tt.want, got)
			}
		})
	}
}

func TestJoinWebhookURL(t *testing.T) {
	tests := []struct {
		name string
		base string
		path string
		want string
	}{
		{
			name: "default path",
			base: "https://example.trycloudflare.com",
			path: "",
			want: "https://example.trycloudflare.com/webhook",
		},
		{
			name: "path without leading slash",
			base: "https://example.trycloudflare.com",
			path: "webhook",
			want: "https://example.trycloudflare.com/webhook",
		},
		{
			name: "base with trailing slash",
			base: "https://example.trycloudflare.com/",
			path: "/webhook",
			want: "https://example.trycloudflare.com/webhook",
		},
		{
			name: "custom path",
			base: "https://example.trycloudflare.com",
			path: "/custom",
			want: "https://example.trycloudflare.com/custom",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := JoinWebhookURL(tt.base, tt.path)
			if got != tt.want {
				t.Fatalf("expected %q, got %q", tt.want, got)
			}
		})
	}
}
