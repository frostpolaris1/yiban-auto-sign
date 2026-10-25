package protocol

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
)

// EncryptPassword 用 RSA PKCS#1 v1.5 加密口令，返回 base64 字符串。
//
// RSA-1024 的密文为 128 字节，base64 后 172 字符、末尾单个 =。
// 公钥不是可用 RSA 公钥时返回错误。
func EncryptPassword(password, publicKeyPEM string) (string, error) {
	block, _ := pem.Decode([]byte(publicKeyPEM))
	if block == nil {
		return "", errors.New("public key PEM is empty")
	}
	var parsed any
	var err error
	parsed, err = x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		parsed, err = x509.ParsePKCS1PublicKey(block.Bytes)
		if err != nil {
			return "", errors.New("public key PEM is not a valid RSA public key")
		}
	}
	key, ok := parsed.(*rsa.PublicKey)
	if !ok {
		return "", errors.New("public key is not RSA")
	}
	ciphertext, err := rsa.EncryptPKCS1v15(rand.Reader, key, []byte(password))
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(ciphertext), nil
}
