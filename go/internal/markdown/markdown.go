// Package markdown converts markdown to Telegram-compatible HTML.
package markdown

import (
	"regexp"
	"strings"
)

// ToHTML converts markdown text to Telegram-compatible HTML.
// Supports: **bold**, *italic*, ```code blocks```, `inline code`
func ToHTML(text string) string {
	// Extract and protect code blocks and inline code first
	var blocks []codeBlock
	var inlines []string

	// Code blocks: ```lang\ncode```
	codeBlockRE := regexp.MustCompile("(?s)```(\\w*)\\n?(.*?)```")
	text = codeBlockRE.ReplaceAllStringFunc(text, func(match string) string {
		parts := codeBlockRE.FindStringSubmatch(match)
		lang, code := "", ""
		if len(parts) >= 2 {
			lang = parts[1]
		}
		if len(parts) >= 3 {
			code = parts[2]
		}
		blocks = append(blocks, codeBlock{lang: lang, code: code})
		return "\x00B" + string(rune('0'+len(blocks)-1)) + "\x00"
	})

	// Inline code: `code`
	inlineCodeRE := regexp.MustCompile("`([^`\\n]+)`")
	text = inlineCodeRE.ReplaceAllStringFunc(text, func(match string) string {
		parts := inlineCodeRE.FindStringSubmatch(match)
		code := ""
		if len(parts) >= 2 {
			code = parts[1]
		}
		inlines = append(inlines, code)
		return "\x00I" + string(rune('0'+len(inlines)-1)) + "\x00"
	})

	// Escape HTML
	text = escapeHTML(text)

	// Bold: **text**
	boldRE := regexp.MustCompile(`\*\*(.+?)\*\*`)
	text = boldRE.ReplaceAllString(text, "<b>$1</b>")

	// Italic: *text* (but not **text**)
	italicRE := regexp.MustCompile(`(?:^|[^*])\*([^*]+)\*(?:[^*]|$)`)
	text = italicRE.ReplaceAllStringFunc(text, func(match string) string {
		// Preserve surrounding characters
		prefix := ""
		suffix := ""
		if len(match) > 0 && match[0] != '*' {
			prefix = string(match[0])
			match = match[1:]
		}
		if len(match) > 0 && match[len(match)-1] != '*' {
			suffix = string(match[len(match)-1])
			match = match[:len(match)-1]
		}
		// Now match should be *content*
		if len(match) >= 2 && match[0] == '*' && match[len(match)-1] == '*' {
			content := match[1 : len(match)-1]
			return prefix + "<i>" + content + "</i>" + suffix
		}
		return prefix + match + suffix
	})

	// Restore code blocks
	for i, block := range blocks {
		placeholder := "\x00B" + string(rune('0'+i)) + "\x00"
		var replacement string
		if block.lang != "" {
			replacement = `<pre><code class="language-` + block.lang + `">` + escapeHTML(strings.TrimSpace(block.code)) + "</code></pre>"
		} else {
			replacement = "<pre>" + escapeHTML(strings.TrimSpace(block.code)) + "</pre>"
		}
		text = strings.Replace(text, placeholder, replacement, 1)
	}

	// Restore inline code
	for i, code := range inlines {
		placeholder := "\x00I" + string(rune('0'+i)) + "\x00"
		replacement := "<code>" + escapeHTML(code) + "</code>"
		text = strings.Replace(text, placeholder, replacement, 1)
	}

	return text
}

type codeBlock struct {
	lang string
	code string
}

func escapeHTML(s string) string {
	s = strings.ReplaceAll(s, "&", "&amp;")
	s = strings.ReplaceAll(s, "<", "&lt;")
	s = strings.ReplaceAll(s, ">", "&gt;")
	return s
}
