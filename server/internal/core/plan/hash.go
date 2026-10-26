package plan

import (
	"crypto/sha256"
	"encoding/binary"
)

// hash01 返回 [0,1) 的确定性均匀量：sha256 前 8 字节取高 53 位。
// 禁用内置 hash（每进程随机化），blake2b 需要引第三方库，
// sha256 是标准库内最稳的选择；分点粒度 2^53 可被 float64 精确表示。
func hash01(s string) float64 {
	sum := sha256.Sum256([]byte(s))
	return float64(binary.BigEndian.Uint64(sum[:8])>>11) / float64(1<<53)
}
