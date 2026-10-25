package pg_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// distinctHash 返回唯一的 32 字节令牌摘要，
// 使「某条形状约束被放过」的突变体失败在形状上，
// 而不是失败在共享的唯一索引上。
func distinctHash(n byte) []byte {
	out := make([]byte, 32)
	out[0] = n
	return out
}

// TestDatabaseConstraintsRejectBadRows 钉住 Go 层会掩盖的 CHECK 与 UNIQUE 约束。
//
// 每条语句都绕过仓储，走独立连接，故**只有数据库能拒绝它**。
// mapError 会改写 SQLSTATE，故这里检查原始 pgx 错误：
// 测试要指出究竟哪条约束被触发。
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
			name: "ck_sessions_ip_hash_shape",
			sql: `INSERT INTO sessions (user_id, token_hash, idle_expires_at, expires_at, ip_hash)
			      VALUES ($1::uuid, $2, now() + interval '30 minutes', now() + interval '12 hours', $3)`,
			args:       []any{owner.ID, distinctHash(3), "203.0.113.5"},
			code:       "23514",
			constraint: "ck_sessions_ip_hash_shape",
		},
		{
			name: "ck_sessions_user_agent_hash_shape",
			sql: `INSERT INTO sessions (user_id, token_hash, idle_expires_at, expires_at, user_agent_hash)
			      VALUES ($1::uuid, $2, now() + interval '30 minutes', now() + interval '12 hours', $3)`,
			args:       []any{owner.ID, distinctHash(4), "Mozilla/5.0"},
			code:       "23514",
			constraint: "ck_sessions_user_agent_hash_shape",
		},
		{
			name: "ck_sessions_ip_hash_shape 63 hex",
			sql: `INSERT INTO sessions (user_id, token_hash, idle_expires_at, expires_at, ip_hash)
			      VALUES ($1::uuid, $2, now() + interval '30 minutes', now() + interval '12 hours', $3)`,
			args:       []any{owner.ID, distinctHash(1), strings.Repeat("ab", 31) + "a"},
			code:       "23514",
			constraint: "ck_sessions_ip_hash_shape",
		},
		{
			name: "ck_sessions_ip_hash_shape 65 hex",
			sql: `INSERT INTO sessions (user_id, token_hash, idle_expires_at, expires_at, ip_hash)
			      VALUES ($1::uuid, $2, now() + interval '30 minutes', now() + interval '12 hours', $3)`,
			args:       []any{owner.ID, distinctHash(2), strings.Repeat("ab", 32) + "a"},
			code:       "23514",
			constraint: "ck_sessions_ip_hash_shape",
		},
		{
			name: "ck_sessions_ip_hash_shape upper hex",
			sql: `INSERT INTO sessions (user_id, token_hash, idle_expires_at, expires_at, ip_hash)
			      VALUES ($1::uuid, $2, now() + interval '30 minutes', now() + interval '12 hours', $3)`,
			args:       []any{owner.ID, distinctHash(5), strings.ToUpper(strings.Repeat("ab", 32))},
			code:       "23514",
			constraint: "ck_sessions_ip_hash_shape",
		},
		{
			name: "ck_sessions_ip_hash_shape short hex",
			sql: `INSERT INTO sessions (user_id, token_hash, idle_expires_at, expires_at, ip_hash)
			      VALUES ($1::uuid, $2, now() + interval '30 minutes', now() + interval '12 hours', $3)`,
			args:       []any{owner.ID, distinctHash(6), strings.Repeat("ab", 31)},
			code:       "23514",
			constraint: "ck_sessions_ip_hash_shape",
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

			// 被拒绝的行不留痕迹，表内容不变。
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
