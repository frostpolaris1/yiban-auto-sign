package pg_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// TestDatabaseConstraintsRejectBadRows pins the CHECK and UNIQUE constraints
// that the Go layer would otherwise hide.
//
// Every statement bypasses the repositories and runs on its own connection, so
// only the database can reject it. mapError rewrites SQLSTATE codes, so the
// raw pgx error is inspected: the test names the exact constraint that fired.
func TestDatabaseConstraintsRejectBadRows(t *testing.T) {
	e := newEnv(t)
	owner := e.createUser("ddl-owner@example.com")
	school := e.createSchool("ddl-school")
	account := e.createAccount(school.ID, owner.ID, "13500135000")

	ctx := e.ctx
	conn, err := pgx.Connect(ctx, e.dsn)
	if err != nil {
		t.Fatalf("open raw connection: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close(context.Background()) })

	validNonce := make([]byte, 12)
	validCiphertext := make([]byte, 32)
	validTokenHash := make([]byte, 32)

	for _, tc := range []struct {
		name       string
		sql        string
		args       []any
		code       string
		constraint string
	}{
		{
			name:       "ck_accounts_phone_shape",
			sql:        `INSERT INTO accounts (school_id, owner_user_id, phone) VALUES ($1::uuid, $2::uuid, '12345')`,
			args:       []any{school.ID, owner.ID},
			code:       "23514",
			constraint: "ck_accounts_phone_shape",
		},
		{
			name:       "uq_accounts_school_phone_live",
			sql:        `INSERT INTO accounts (school_id, owner_user_id, phone) VALUES ($1::uuid, $2::uuid, $3)`,
			args:       []any{school.ID, owner.ID, "13500135000"},
			code:       "23505",
			constraint: "uq_accounts_school_phone_live",
		},
		{
			name: "ck_credentials_nonce_len",
			sql: `INSERT INTO credentials (account_id, school_id, kid, nonce, ciphertext)
			      VALUES ($1::uuid, $2::uuid, '0123456789abcdef', $3, $4)`,
			args:       []any{account.ID, school.ID, make([]byte, 11), validCiphertext},
			code:       "23514",
			constraint: "ck_credentials_nonce_len",
		},
		{
			name: "ck_credentials_kid_shape",
			sql: `INSERT INTO credentials (account_id, school_id, kid, nonce, ciphertext)
			      VALUES ($1::uuid, $2::uuid, 'not-a-kid', $3, $4)`,
			args:       []any{account.ID, school.ID, validNonce, validCiphertext},
			code:       "23514",
			constraint: "ck_credentials_kid_shape",
		},
		{
			name:       "ck_users_email_lower",
			sql:        `INSERT INTO users (email, password_hash) VALUES ('MiXeD@Example.com', 'x')`,
			code:       "23514",
			constraint: "ck_users_email_lower",
		},
		{
			name:       "ck_schools_code_shape",
			sql:        `INSERT INTO schools (code, name) VALUES ('Bad Code', 'x')`,
			code:       "23514",
			constraint: "ck_schools_code_shape",
		},
		{
			name: "ck_accounts_review_pair",
			sql: `INSERT INTO accounts (school_id, owner_user_id, phone, reviewed_at)
			      VALUES ($1::uuid, $2::uuid, '13500135009', now())`,
			args:       []any{school.ID, owner.ID},
			code:       "23514",
			constraint: "ck_accounts_review_pair",
		},
		{
			name: "ck_accounts_reject_reason",
			sql: `INSERT INTO accounts (school_id, owner_user_id, phone, status, reject_reason)
			      VALUES ($1::uuid, $2::uuid, '13500135008', 'rejected', '')`,
			args:       []any{school.ID, owner.ID},
			code:       "23514",
			constraint: "ck_accounts_reject_reason",
		},
		{
			name: "ck_sessions_token_len",
			sql: `INSERT INTO sessions (user_id, token_hash, idle_expires_at, expires_at)
			      VALUES ($1::uuid, $2, now() + interval '30 minutes', now() + interval '12 hours')`,
			args:       []any{owner.ID, make([]byte, 31)},
			code:       "23514",
			constraint: "ck_sessions_token_len",
		},
		{
			name: "ck_sessions_expiry",
			sql: `INSERT INTO sessions (user_id, token_hash, idle_expires_at, expires_at)
			      VALUES ($1::uuid, $2, now() + interval '13 hours', now() + interval '12 hours')`,
			args:       []any{owner.ID, validTokenHash},
			code:       "23514",
			constraint: "ck_sessions_expiry",
		},
		{
			name: "ck_credentials_ct_len",
			sql: `INSERT INTO credentials (account_id, school_id, kid, nonce, ciphertext)
			      VALUES ($1::uuid, $2::uuid, '0123456789abcdef', $3, $4)`,
			args:       []any{account.ID, school.ID, validNonce, make([]byte, 8)},
			code:       "23514",
			constraint: "ck_credentials_ct_len",
		},
		{
			name: "ck_credentials_aad_version",
			sql: `INSERT INTO credentials (account_id, school_id, kid, nonce, ciphertext, aad_version)
			             VALUES ($1::uuid, $2::uuid, '0123456789abcdef', $3, $4, 2)`,
			args:       []any{account.ID, school.ID, validNonce, validCiphertext},
			code:       "23514",
			constraint: "ck_credentials_aad_version",
		},
		{
			name:       "uq_users_email_live",
			sql:        `INSERT INTO users (email, password_hash) VALUES ($1, 'x')`,
			args:       []any{owner.Email},
			code:       "23505",
			constraint: "uq_users_email_live",
		},
		{
			name: "ck_accounts_reject_reason_len",
			sql: `INSERT INTO accounts (school_id, owner_user_id, phone, status, reject_reason)
			      VALUES ($1::uuid, $2::uuid, '13500135007', 'rejected', $3)`,
			args:       []any{school.ID, owner.ID, strings.Repeat("r", 201)},
			code:       "23514",
			constraint: "ck_accounts_reject_reason_len",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := conn.Exec(ctx, tc.sql, tc.args...); err == nil {
				t.Fatalf("%s did not fire; the statement was accepted", tc.constraint)
			} else {
				var pgErr *pgconn.PgError
				if !errors.As(err, &pgErr) {
					t.Fatalf("%s: want a PostgreSQL error, got %v", tc.constraint, err)
				}
				if pgErr.Code != tc.code {
					t.Fatalf("%s: SQLSTATE is %s; want %s", tc.constraint, pgErr.Code, tc.code)
				}
				if pgErr.ConstraintName != tc.constraint {
					t.Fatalf("blocked by %q; want %q", pgErr.ConstraintName, tc.constraint)
				}
			}

			// A rejected row leaves the table unchanged.
			if tc.constraint == "uq_accounts_school_phone_live" {
				count := e.scalarInt("SELECT count(*) FROM accounts WHERE school_id = $1::uuid AND phone = $2",
					school.ID, "13500135000")
				if count != 1 {
					t.Fatalf("phone row count is %d after the rejected duplicate; want 1", count)
				}
			}
		})
	}
}
