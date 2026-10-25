// Package egress is the only definition of an egress identity.
package egress

import (
	"net/url"
	"strings"
)

const (
	// Direct is the identity of a direct connection.
	Direct = "direct"
	// Unparseable is the identity of a malformed proxy string.
	Unparseable = "<unparseable>"
)

// Identity normalizes a proxy string to scheme://host[:port] without userinfo.
func Identity(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return Direct
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return Unparseable
	}
	u.User = nil
	return u.Scheme + "://" + u.Host
}
