# textutil

Small text helpers: `Truncate`, `Words` and `Wrap`.

## Wrap

`Wrap(s string, width int) []string` breaks `s` into lines of at most `width`
runes, splitting only at whitespace. Words are separated by single spaces in
the output. A word longer than `width` is put on a line of its own, unbroken.
Empty or whitespace-only input returns an empty (non-nil) slice. `width <= 0`
returns all words joined by single spaces as one line.
