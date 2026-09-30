// Package semver compares semantic version strings.
//
// Compare follows Semantic Versioning 2.0.0 precedence:
//   - MAJOR.MINOR.PATCH are compared numerically, left to right.
//   - A version with a pre-release part has LOWER precedence than the same
//     version without one: 1.0.0-alpha < 1.0.0.
//   - Pre-release identifiers (split on ".") are compared left to right.
//     Identifiers made only of digits are compared numerically; others are
//     compared in ASCII order; a numeric identifier is lower than a
//     non-numeric one; if all shared identifiers are equal, the version with
//     more identifiers is higher: 1.0.0-alpha < 1.0.0-alpha.1.
//   - Build metadata ("+...") is ignored.
//
// Compare returns -1, 0 or +1. It returns an error for an invalid version.
package semver

import (
	"fmt"
	"strconv"
	"strings"
)

type version struct {
	core [3]int
	pre  []string
}

func parse(s string) (version, error) {
	var v version
	if i := strings.IndexByte(s, '+'); i >= 0 {
		s = s[:i]
	}
	core := s
	if i := strings.IndexByte(s, '-'); i >= 0 {
		core = s[:i]
		v.pre = strings.Split(s[i+1:], ".")
		for _, id := range v.pre {
			if id == "" {
				return v, fmt.Errorf("invalid version %q", s)
			}
		}
	}
	parts := strings.Split(core, ".")
	if len(parts) != 3 {
		return v, fmt.Errorf("invalid version %q", s)
	}
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 {
			return v, fmt.Errorf("invalid version %q", s)
		}
		v.core[i] = n
	}
	return v, nil
}

func isNumeric(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return s != ""
}

func cmpInt(a, b int) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

func compareIdentifier(a, b string) int {
	an, bn := isNumeric(a), isNumeric(b)
	switch {
	case an && bn:
		x, _ := strconv.Atoi(a)
		y, _ := strconv.Atoi(b)
		return cmpInt(x, y)
	case an:
		return -1
	case bn:
		return 1
	}
	return strings.Compare(a, b)
}

// Compare returns -1 if a < b, 0 if a == b and +1 if a > b.
func Compare(a, b string) (int, error) {
	va, err := parse(a)
	if err != nil {
		return 0, err
	}
	vb, err := parse(b)
	if err != nil {
		return 0, err
	}
	for i := 0; i < 3; i++ {
		if c := cmpInt(va.core[i], vb.core[i]); c != 0 {
			return c, nil
		}
	}
	switch {
	case len(va.pre) == 0 && len(vb.pre) == 0:
		return 0, nil
	case len(va.pre) == 0:
		return 1, nil
	case len(vb.pre) == 0:
		return -1, nil
	}
	for i := 0; i < len(va.pre) && i < len(vb.pre); i++ {
		if c := compareIdentifier(va.pre[i], vb.pre[i]); c != 0 {
			return c, nil
		}
	}
	return cmpInt(len(va.pre), len(vb.pre)), nil
}
