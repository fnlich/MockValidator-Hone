// Package textutil holds small text helpers.
package textutil

import "strings"

// Truncate returns s cut to at most n runes, with "..." appended when cut.
// n <= 3 returns the first n runes without an ellipsis.
func Truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	if n <= 3 {
		return string(r[:n])
	}
	return string(r[:n-3]) + "..."
}

// Words splits s on runs of whitespace.
func Words(s string) []string {
	return strings.Fields(s)
}

// Wrap breaks s into lines of at most width runes, splitting only at
// whitespace. Words are separated by single spaces in the output. A word
// longer than width is put on a line of its own, unbroken. Empty or
// whitespace-only input returns an empty (non-nil) slice. width <= 0 returns
// all words joined by single spaces as one line.
func Wrap(s string, width int) []string {
	words := strings.Fields(s)
	lines := []string{}
	if len(words) == 0 {
		return lines
	}
	if width <= 0 {
		return []string{strings.Join(words, " ")}
	}
	current := words[0]
	for _, w := range words[1:] {
		if len([]rune(current))+1+len([]rune(w)) <= width {
			current += " " + w
			continue
		}
		lines = append(lines, current)
		current = w
	}
	return append(lines, current)
}
