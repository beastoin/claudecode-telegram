package connector

import (
	"html/template"
	"strings"
	"testing"
)

func TestWebConnector_IsAuthResponseText(t *testing.T) {
	t.Parallel()

	tests := []struct {
		text string
		want bool
	}{
		{"Auth required for mon\nURL: https://...", true},
		{"Auth required for worker-x\nReply...", true},
		{"Hello world", false},
		{"Authentication failed", false},
		{"", false},
	}
	for _, tt := range tests {
		if got := IsAuthResponseText(tt.text); got != tt.want {
			t.Errorf("IsAuthResponseText(%q) = %v, want %v", tt.text[:min(30, len(tt.text))], got, tt.want)
		}
	}
}

func TestRenderMarkdown(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name string
		in   string
		want []string
	}{
		{"bold", "**hello**", []string{"<strong>hello</strong>"}},
		{"italic", " *world* ", []string{"<em>world</em>"}},
		{"inline code", "`foo()`", []string{"<code>foo()</code>"}},
		{"strikethrough", "~~old~~", []string{"<del>old</del>"}},
		{"header", "## Title", []string{"<h2", "Title", "</h2>"}},
		{"bullet list", "- item one\n- item two", []string{"• item one", "• item two"}},
		{"numbered list", "1. first\n2. second", []string{"1. first", "2. second"}},
		{"hr", "---", []string{"<hr"}},
		{"md link", "[docs](https://example.com)", []string{`href="https://example.com"`, ">docs</a>"}},
		{"bare url", "visit https://example.com ok", []string{`href="https://example.com"`, ">https://example.com</a>"}},
		{"code block", "```go\nfmt.Println()\n```", []string{"<pre><code", "fmt.Println()", "</code></pre>"}},
		{"blockquote", "> important note", []string{"<blockquote", "important note"}},
		{"html escape", "<script>alert(1)</script>", []string{"&lt;script&gt;"}},
		{"no double link", "[text](https://a.com) and https://b.com", []string{">text</a>", ">https://b.com</a>"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := string(RenderMarkdown(tt.in))
			for _, w := range tt.want {
				if !strings.Contains(got, w) {
					t.Errorf("RenderMarkdown(%q) = %q, want to contain %q", tt.in, got, w)
				}
			}
		})
	}
}

func TestRenderMarkdown_NoDoubleWrapURLs(t *testing.T) {
	t.Parallel()

	input := "[click here](https://example.com/page) and also https://other.com/path"
	got := string(RenderMarkdown(input))

	if strings.Count(got, "example.com/page") != 1 {
		t.Fatalf("markdown link URL should appear once (in href only), got: %s", got)
	}
	if !strings.Contains(got, ">click here</a>") {
		t.Fatalf("markdown link text should be 'click here', got: %s", got)
	}
	if !strings.Contains(got, ">https://other.com/path</a>") {
		t.Fatalf("bare URL should be linked, got: %s", got)
	}
}

func TestRenderAuthCard(t *testing.T) {
	t.Parallel()

	html := renderAuthCard("42", "mon", "https://claude.ai/oauth/test")

	if !strings.Contains(html, "msg auth") {
		t.Fatal("should have auth class")
	}
	if !strings.Contains(html, `href="https://claude.ai/oauth/test"`) {
		t.Fatal("should have clickable URL")
	}
	if !strings.Contains(html, `value="42"`) {
		t.Fatal("should have reply_to_id hidden input")
	}
	if !strings.Contains(html, "hx-post") {
		t.Fatal("should have HTMX form")
	}
}

func TestRenderAuthStatus(t *testing.T) {
	t.Parallel()

	tests := []struct {
		status string
		want   []string
	}{
		{"submitting", []string{"⏳", "status"}},
		{"success", []string{"✓", "success"}},
		{"failed", []string{"✗", "error"}},
	}
	for _, tt := range tests {
		html := renderAuthStatus(tt.status, "test detail")
		for _, w := range tt.want {
			if !strings.Contains(html, w) {
				t.Errorf("renderAuthStatus(%q) missing %q, got: %s", tt.status, w, html)
			}
		}
	}
}

func TestRenderCommandBar(t *testing.T) {
	t.Parallel()

	bar := renderCommandBar([]CommandSpec{
		{Name: "help", Description: "List commands"},
		{Name: "status", Description: "Show status"},
	})
	html := string(bar)

	if !strings.Contains(html, `sendCmd('help')`) {
		t.Fatal("should have sendCmd for help")
	}
	if !strings.Contains(html, `sendCmd('status')`) {
		t.Fatal("should have sendCmd for status")
	}
	if !strings.Contains(html, `title="List commands"`) {
		t.Fatal("should have description tooltip")
	}
}

func TestRenderCommandBar_Empty(t *testing.T) {
	t.Parallel()

	bar := renderCommandBar(nil)
	if bar != "" {
		t.Fatalf("empty commands should return empty string, got %q", bar)
	}
}

var _ = template.HTML("")
