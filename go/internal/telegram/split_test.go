package telegram

import (
	"strings"
	"testing"
)

func TestSplitMessageShort(t *testing.T) {
	text := "hello"
	chunks := SplitMessage(text)
	if len(chunks) != 1 {
		t.Fatalf("expected 1 chunk, got %d", len(chunks))
	}
	if chunks[0] != text {
		t.Fatalf("expected chunk to equal input")
	}
}

func TestSplitMessageParagraphBreaks(t *testing.T) {
	part1 := strings.Repeat("a", 3000)
	part2 := strings.Repeat("b", 2000)
	text := part1 + "\n\n" + part2

	chunks := SplitMessage(text)
	if len(chunks) != 2 {
		t.Fatalf("expected 2 chunks, got %d", len(chunks))
	}
	if chunks[0] != part1+"\n\n" {
		t.Fatalf("expected first chunk to end at paragraph break")
	}
	if chunks[1] != part2 {
		t.Fatalf("expected second chunk to contain remainder")
	}
	if strings.Join(chunks, "") != text {
		t.Fatalf("expected chunks to reassemble original text")
	}
}

func TestSplitMessageSingleNewline(t *testing.T) {
	part1 := strings.Repeat("a", 3000)
	part2 := strings.Repeat("b", 2000)
	text := part1 + "\n" + part2

	chunks := SplitMessage(text)
	if len(chunks) != 2 {
		t.Fatalf("expected 2 chunks, got %d", len(chunks))
	}
	if chunks[0] != part1+"\n" {
		t.Fatalf("expected first chunk to end at single newline")
	}
	if chunks[1] != part2 {
		t.Fatalf("expected second chunk to contain remainder")
	}
}

func TestSplitMessageSpaceFallback(t *testing.T) {
	part1 := strings.Repeat("a", 3000)
	part2 := strings.Repeat("b", 2000)
	text := part1 + " " + part2

	chunks := SplitMessage(text)
	if len(chunks) != 2 {
		t.Fatalf("expected 2 chunks, got %d", len(chunks))
	}
	if chunks[0] != part1+" " {
		t.Fatalf("expected first chunk to end at space")
	}
	if chunks[1] != part2 {
		t.Fatalf("expected second chunk to contain remainder")
	}
}

func TestSplitMessageHardCut(t *testing.T) {
	text := strings.Repeat("a", MaxMessageLength+10)
	chunks := SplitMessage(text)
	if len(chunks) != 2 {
		t.Fatalf("expected 2 chunks, got %d", len(chunks))
	}
	if len(chunks[0]) != MaxMessageLength {
		t.Fatalf("expected first chunk length %d, got %d", MaxMessageLength, len(chunks[0]))
	}
	if len(chunks[1]) != 10 {
		t.Fatalf("expected second chunk length 10, got %d", len(chunks[1]))
	}
	if strings.Join(chunks, "") != text {
		t.Fatalf("expected chunks to reassemble original text")
	}
}

func TestSplitMessageEdgeCases(t *testing.T) {
	chunks := SplitMessage("")
	if len(chunks) != 0 {
		t.Fatalf("expected 0 chunks for empty input, got %d", len(chunks))
	}

	text := strings.Repeat("a", MaxMessageLength)
	chunks = SplitMessage(text)
	if len(chunks) != 1 {
		t.Fatalf("expected 1 chunk for exact limit, got %d", len(chunks))
	}
	if chunks[0] != text {
		t.Fatalf("expected chunk to equal input for exact limit")
	}
}
