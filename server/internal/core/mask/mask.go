// Package mask is the only place that turns personal data into display text.
//
// Every function is idempotent: masking an already masked value returns it
// unchanged. Callers must not reimplement these rules.
package mask

import (
	"regexp"
	"strings"
)

var (
	phoneInText = regexp.MustCompile(`\b1\d{10}\b`)
	emailInText = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
)

// MaskPhone keeps the first three and last four digits.
func MaskPhone(s string) string {
	if strings.Contains(s, "****") || len(s) != 11 || s[0] != '1' {
		return s
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return s
		}
	}
	return s[:3] + "****" + s[7:]
}

// MaskEmail keeps the first character and the domain.
func MaskEmail(s string) string {
	at := strings.LastIndexByte(s, '@')
	if at <= 0 {
		return s
	}
	local := s[:at]
	if strings.Contains(local, "***") {
		return s
	}
	return local[:1] + "***" + s[at:]
}

// MaskDevice keeps the first two and last two characters.
func MaskDevice(s string) string {
	if strings.Contains(s, "****") {
		return s
	}
	if len(s) <= 4 {
		return "****"
	}
	return s[:2] + "****" + s[len(s)-2:]
}

// MaskText masks phone numbers and email addresses inside free text.
func MaskText(s string) string {
	s = emailInText.ReplaceAllStringFunc(s, MaskEmail)
	return phoneInText.ReplaceAllStringFunc(s, MaskPhone)
}
