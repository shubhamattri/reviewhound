package strutil

import "strings"

func Upper(s string) string {
	return strings.ToUpper(s)
}

func Trim(s string) string {
	return strings.TrimSpace(s)
}
