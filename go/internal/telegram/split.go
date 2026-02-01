package telegram

import "strings"

const MaxMessageLength = 4096

func SplitMessage(text string) []string {
	if text == "" {
		return []string{}
	}

	chunks := make([]string, 0, (len(text)/MaxMessageLength)+1)
	for len(text) > 0 {
		if len(text) <= MaxMessageLength {
			chunks = append(chunks, text)
			break
		}

		cut := splitIndex(text, MaxMessageLength)
		if cut <= 0 || cut > len(text) {
			cut = MaxMessageLength
		}

		chunks = append(chunks, text[:cut])
		text = text[cut:]
	}

	return chunks
}

func splitIndex(text string, limit int) int {
	if len(text) <= limit {
		return len(text)
	}

	window := text[:limit]
	for _, delim := range []string{"\n\n", "\n", " "} {
		if idx := strings.LastIndex(window, delim); idx != -1 {
			return idx + len(delim)
		}
	}

	return limit
}
