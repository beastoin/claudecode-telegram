package markdown

import "testing"

func TestToHTML(t *testing.T) {
	tests := []struct {
		name     string
		input    string
		expected string
	}{
		{
			name:     "plain text",
			input:    "Hello world",
			expected: "Hello world",
		},
		{
			name:     "bold",
			input:    "This is **bold** text",
			expected: "This is <b>bold</b> text",
		},
		{
			name:     "italic",
			input:    "This is *italic* text",
			expected: "This is <i>italic</i> text",
		},
		{
			name:     "inline code",
			input:    "Use `fmt.Println` here",
			expected: "Use <code>fmt.Println</code> here",
		},
		{
			name:     "code block no lang",
			input:    "```\nfunc main() {}\n```",
			expected: "<pre>func main() {}</pre>",
		},
		{
			name:     "code block with lang",
			input:    "```go\nfunc main() {}\n```",
			expected: `<pre><code class="language-go">func main() {}</code></pre>`,
		},
		{
			name:     "html escaping",
			input:    "Use <div> and &amp;",
			expected: "Use &lt;div&gt; and &amp;amp;",
		},
		{
			name:     "code block with html",
			input:    "```html\n<div>test</div>\n```",
			expected: `<pre><code class="language-html">&lt;div&gt;test&lt;/div&gt;</code></pre>`,
		},
		{
			name:     "mixed formatting",
			input:    "**Bold** and *italic* with `code`",
			expected: "<b>Bold</b> and <i>italic</i> with <code>code</code>",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := ToHTML(tt.input)
			if result != tt.expected {
				t.Errorf("ToHTML(%q) = %q, want %q", tt.input, result, tt.expected)
			}
		})
	}
}

func TestEscapeHTML(t *testing.T) {
	tests := []struct {
		input    string
		expected string
	}{
		{"hello", "hello"},
		{"<div>", "&lt;div&gt;"},
		{"a & b", "a &amp; b"},
		{"<script>alert('xss')</script>", "&lt;script&gt;alert('xss')&lt;/script&gt;"},
	}

	for _, tt := range tests {
		result := escapeHTML(tt.input)
		if result != tt.expected {
			t.Errorf("escapeHTML(%q) = %q, want %q", tt.input, result, tt.expected)
		}
	}
}
