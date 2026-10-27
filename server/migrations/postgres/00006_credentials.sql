-- 00006_credentials: encrypted credentials, one row per account.
--
-- The composite foreign key (account_id, school_id) makes the credential
-- school always equal the account school, so a copied ciphertext cannot be
-- decrypted under another account without the AES-GCM AAD check failing too.
--
-- Governance: migrations are forward-only. There is no Down section.

-- +goose Up
CREATE TABLE credentials (
    account_id       uuid PRIMARY KEY,
    school_id        uuid NOT NULL,
    alg              text NOT NULL DEFAULT 'AES-256-GCM',
    kid              text NOT NULL,
    aad_version      smallint NOT NULL DEFAULT 1,
    nonce            bytea NOT NULL,
    ciphertext       bytea NOT NULL,
    version          integer NOT NULL DEFAULT 1,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    rekeyed_at       timestamptz,
    last_verified_at timestamptz,
    CONSTRAINT fk_credentials_account_school
        FOREIGN KEY (account_id, school_id)
        REFERENCES accounts (id, school_id) ON DELETE CASCADE,
    CONSTRAINT ck_credentials_alg CHECK (alg = 'AES-256-GCM'),
    CONSTRAINT ck_credentials_nonce_len CHECK (octet_length(nonce) = 12),
    CONSTRAINT ck_credentials_ct_len CHECK (octet_length(ciphertext) BETWEEN 16 AND 8192),
    CONSTRAINT ck_credentials_kid_shape CHECK (kid ~ '^[0-9a-f]{16}$'),
    CONSTRAINT ck_credentials_aad_version CHECK (aad_version = 1),
    CONSTRAINT ck_credentials_version CHECK (version >= 1)
);

CREATE TRIGGER trg_credentials_updated_at BEFORE UPDATE ON credentials
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
