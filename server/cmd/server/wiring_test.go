package main

import (
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
)

// Compile-time wiring checks.
//
// The ports live in internal/app and the implementations in
// internal/adapters/pg. canonical.tsv forbids the adapter from importing app,
// so these assertions are the place where the two sides must match. A
// signature change on either side breaks the build here, not at first use.
//
// This file asserts types only. It opens no connection and starts no server.
var (
	_ app.TxManager             = (*pg.DB)(nil)
	_ appuser.Repo              = (*pg.UserRepo)(nil)
	_ appauth.SessionRepo       = (*pg.SessionRepo)(nil)
	_ appauth.PasswordResetRepo = (*pg.PasswordResetRepo)(nil)
	_ appschool.Repo            = (*pg.SchoolRepo)(nil)
	_ appschool.AdminRepo       = (*pg.AdminSchoolRepo)(nil)
	_ appaccount.Repo           = (*pg.AccountRepo)(nil)
	_ appaudit.Repo             = (*pg.AuditRepo)(nil)
)
