// Package migrations embeds the PostgreSQL migrations so the service binary
// carries its own schema version.
//
// Most migrations ship a Down block, and goose can run it in a development
// database. Production rollback means restoring a backup: a Down that edits
// data-bearing columns is never executed against a live deployment.
package migrations

import "embed"

// Dir is the directory inside Files that holds the SQL files.
const Dir = "postgres"

// Files holds every migration. The embed pattern is checked at build time, so
// a missing directory is a compile error, not an empty migration set.
//
//go:embed postgres/*.sql
var Files embed.FS
