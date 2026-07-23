package connector

import (
	"strings"
	"testing"
)

func FuzzSplitTelegramMessage(f *testing.F) {
	f.Add("hello world", 10)
	f.Add(strings.Repeat("x", 5000), 4096)
	f.Add("line1\n\nline2 with spaces", 12)
	f.Add("", 100)
	f.Add("abc", 1)
	f.Add("a b c", 3)

	f.Fuzz(func(t *testing.T, text string, maxLen int) {
		if maxLen <= 0 || maxLen > 8192 {
			t.Skip()
		}
		chunks := splitTelegramMessage(text, maxLen)
		for i, chunk := range chunks {
			if len(chunk) > maxLen {
				t.Fatalf("chunk %d len = %d, want <= %d", i, len(chunk), maxLen)
			}
		}
		if text != "" && len(chunks) == 0 {
			t.Fatal("non-empty input produced no chunks")
		}
	})
}

func FuzzStripHTML(f *testing.F) {
	f.Add("<b>bold</b>")
	f.Add("&lt;test&amp;foo&gt;")
	f.Add("no tags")
	f.Add("<a href=\"x\">link</a>")
	f.Add("")

	f.Fuzz(func(t *testing.T, s string) {
		_ = stripHTML(s)
	})
}
