package connector

import (
	"fmt"
	"html"
	"html/template"
	"regexp"
	"strings"
)

var (
	mdCodeBlock = regexp.MustCompile("(?s)```(\\w*)\\n?(.*?)```")
	mdInline    = regexp.MustCompile("`([^`\\n]+)`")
	mdBold      = regexp.MustCompile(`\*\*(.+?)\*\*`)
	mdItalic    = regexp.MustCompile(`(?:^|\s)\*([^*\n]+)\*(?:\s|$|[.,;:!?)])`)
	mdStrike    = regexp.MustCompile(`~~(.+?)~~`)
	mdHeader    = regexp.MustCompile(`(?m)^(#{1,3})\s+(.+)$`)
	mdBullet    = regexp.MustCompile(`(?m)^[-*]\s+(.+)$`)
	mdNumList   = regexp.MustCompile(`(?m)^(\d+)\.\s+(.+)$`)
	mdHR        = regexp.MustCompile(`(?m)^---+$`)
	mdLink      = regexp.MustCompile(`\[([^\]]+)\]\((https?://[^)]+)\)`)
	mdBareURL   = regexp.MustCompile(`https?://[^\s<"')\]]+`)
	mdATag      = regexp.MustCompile(`<a [^>]+>.*?</a>`)
)

func RenderMarkdown(text string) template.HTML {
	h := html.EscapeString(text)

	h = mdCodeBlock.ReplaceAllStringFunc(h, func(m string) string {
		parts := mdCodeBlock.FindStringSubmatch(m)
		code := strings.TrimRight(parts[2], "\n")
		cls := ""
		if parts[1] != "" {
			cls = ` class="lang-` + parts[1] + `"`
		}
		return `<pre><code` + cls + `>` + code + `</code></pre>`
	})

	h = mdInline.ReplaceAllString(h, `<code>$1</code>`)
	h = mdBold.ReplaceAllString(h, `<strong>$1</strong>`)

	h = mdItalic.ReplaceAllStringFunc(h, func(m string) string {
		sub := mdItalic.FindStringSubmatch(m)
		prefix := ""
		if len(m) > 0 && m[0] != '*' {
			prefix = string(m[0])
		}
		suffix := ""
		if len(m) > 0 && m[len(m)-1] != '*' {
			suffix = string(m[len(m)-1])
		}
		return prefix + `<em>` + sub[1] + `</em>` + suffix
	})

	h = mdStrike.ReplaceAllString(h, `<del>$1</del>`)

	h = mdHeader.ReplaceAllStringFunc(h, func(m string) string {
		parts := mdHeader.FindStringSubmatch(m)
		n := len(parts[1])
		size := 20 - n*2
		return fmt.Sprintf(`<h%d style="font-size:%dpx;margin:8px 0 4px;font-weight:600">%s</h%d>`, n, size, parts[2], n)
	})

	h = strings.ReplaceAll(h, "\n&gt; ", "\n> ")
	if strings.HasPrefix(h, "&gt; ") {
		h = "> " + h[5:]
	}
	h = regexp.MustCompile(`(?m)^>\s?(.+)$`).ReplaceAllString(h,
		`<blockquote style="border-left:3px solid #30363d;padding:2px 0 2px 12px;color:#8b949e;margin:4px 0">$1</blockquote>`)

	h = mdBullet.ReplaceAllString(h, `<div style="padding-left:16px">• $1</div>`)
	h = mdNumList.ReplaceAllString(h, `<div style="padding-left:16px">$1. $2</div>`)
	h = mdHR.ReplaceAllString(h, `<hr style="border:none;border-top:1px solid #30363d;margin:8px 0">`)
	h = mdLink.ReplaceAllString(h, `<a href="$2" target="_blank" rel="noopener">$1</a>`)
	h = linkBareURLs(h)

	return template.HTML(h)
}

func linkBareURLs(h string) string {
	existingTags := mdATag.FindAllStringIndex(h, -1)
	if len(existingTags) == 0 {
		return mdBareURL.ReplaceAllString(h, `<a href="$0" target="_blank" rel="noopener">$0</a>`)
	}

	var b strings.Builder
	cursor := 0
	for _, loc := range existingTags {
		chunk := h[cursor:loc[0]]
		b.WriteString(mdBareURL.ReplaceAllString(chunk, `<a href="$0" target="_blank" rel="noopener">$0</a>`))
		b.WriteString(h[loc[0]:loc[1]])
		cursor = loc[1]
	}
	b.WriteString(mdBareURL.ReplaceAllString(h[cursor:], `<a href="$0" target="_blank" rel="noopener">$0</a>`))
	return b.String()
}
