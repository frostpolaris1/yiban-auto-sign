// Package migrations embeds the PostgreSQL migrations so the service binary
// carries its own schema version.
//
// Forward-only: there are no Down files. Rollback means restoring a backup.
package migrations

import "embed"

// Dir is the directory inside Files that holds the SQL files.
const Dir = "postgres"

// Files holds every migration. The embed pattern is checked at build time, so
// a missing directory is a compile error, not an empty migration set.
//
//go:embed postgres/*.sql
var Files embed.FS
