package retry

import "time"

// DefaultAfter 是相邻两次尝试的基础间隔（缺省 60 秒）：
// 防紧邻重试被识别为连击。旧名册 YIBAN_RETRY_MIN_INTERVAL 的同语义缺省。
const DefaultAfter = 60 * time.Second
