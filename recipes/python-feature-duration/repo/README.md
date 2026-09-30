# durations

`format_duration(seconds)` and `parse_duration(text)`.

## parse_duration(text)

Parses a duration made of one or more `<digits><unit>` parts with units `h`,
`m` and `s`, in that order, each at most once: `"1h30m"`, `"45s"`,
`"2h5s"`. Returns whole seconds as an int. Anything else (empty text,
whitespace, a unit without digits, digits without a unit, a repeated or
out-of-order unit, non-ASCII digits) raises
`ValueError("invalid duration: <repr of text>")`.

`parse_duration(format_duration(n)) == n` for every n >= 0.
