// DSN 的脱敏目标提取与错误文本清洗。
//
// # 口径
// 自检报告可以说明「连不上哪台主机、哪个库」，但**绝不**打印口令。
// 本文件是唯一处理 DSN 原文的位置：
//   - dsnTarget 只从 DSN 里取 host:port/dbname，返回值不含 userinfo；
//   - sanitizeText 在错误文本里抹掉口令（原文、百分号编码与 password= 形状）。
//
// 解析失败一律返回静态错误，不回显输入（回显就等于打印口令）。
package updatecheck

import (
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strings"
)

// keywordPasswordRE 抹掉错误文本里的 password=... 形状（含引号与 URI 查询串）。
var keywordPasswordRE = regexp.MustCompile(`(?i)password\s*=\s*(?:'[^']*'|"[^"]*"|[^\s&;]+)`)

// uriUserinfoRE 抹掉 `://user:pass@` 形状的 userinfo。
var uriUserinfoRE = regexp.MustCompile(`://[^/\s:@]+:[^/\s@]*@`)

// dsnTarget 返回脱敏目标 `host:port/dbname`。
// 支持 libpq 的 URI 与 keyword/value 两种形态；其他形态返回错误。
func dsnTarget(dsn string) (string, error) {
	dsn = strings.TrimSpace(dsn)
	if dsn == "" {
		return "", errors.New("DSN 为空")
	}
	if strings.Contains(dsn, "://") {
		return uriTarget(dsn)
	}
	if strings.Contains(dsn, "=") {
		return keywordTarget(dsn)
	}
	return "", errors.New("DSN 必须是 postgres:// URI 或 key=value 形态")
}

// uriTarget 从 URI DSN 取主机与库名。
func uriTarget(dsn string) (string, error) {
	u, err := url.Parse(dsn)
	if err != nil {
		return "", errors.New("DSN URI 无法解析")
	}
	host := u.Host
	if host == "" {
		// libpq 允许 host 放在查询串里（unix socket）。
		if h := u.Query().Get("host"); h != "" {
			host = h
		} else {
			host = "本地套接字"
		}
	}
	db := strings.TrimPrefix(u.Path, "/")
	if db == "" {
		db = "(未指定库)"
	}
	return host + "/" + db, nil
}

// keywordTarget 从 keyword/value DSN 取主机与库名。
func keywordTarget(dsn string) (string, error) {
	fields, err := splitKeywordFields(dsn)
	if err != nil {
		return "", err
	}
	values := map[string]string{}
	for _, f := range fields {
		if _, seen := values[f.key]; seen {
			return "", fmt.Errorf("DSN 字段 %s 重复", f.key)
		}
		values[f.key] = f.value
	}
	host := values["host"]
	if host == "" {
		host = values["hostaddr"]
	}
	if host == "" {
		host = "本地套接字"
	}
	if port := values["port"]; port != "" {
		host += ":" + port
	}
	db := values["dbname"]
	if db == "" {
		db = "(未指定库)"
	}
	return host + "/" + db, nil
}

// keywordField 是一个 libpq 键值对。
type keywordField struct {
	key   string
	value string
}

// splitKeywordFields 按 libpq 词法切分 keyword/value DSN：
// `key = value` 两侧可带空白，值可用单引号包住空白，引号内反斜杠转义。
func splitKeywordFields(dsn string) ([]keywordField, error) {
	var fields []keywordField
	i := 0
	n := len(dsn)
	for i < n {
		for i < n && isSpace(dsn[i]) {
			i++
		}
		if i >= n {
			break
		}
		keyStart := i
		for i < n && !isSpace(dsn[i]) && dsn[i] != '=' {
			i++
		}
		key := dsn[keyStart:i]
		if key == "" {
			return nil, errors.New("DSN 键名为空")
		}
		for i < n && isSpace(dsn[i]) {
			i++
		}
		if i >= n || dsn[i] != '=' {
			return nil, errors.New("DSN 字段缺少 '='")
		}
		i++
		for i < n && isSpace(dsn[i]) {
			i++
		}
		if i >= n {
			return nil, errors.New("DSN 字段缺少值")
		}
		var value string
		if dsn[i] == '\'' {
			i++
			var b strings.Builder
			closed := false
			for i < n {
				c := dsn[i]
				if c == '\\' && i+1 < n {
					b.WriteByte(dsn[i+1])
					i += 2
					continue
				}
				if c == '\'' {
					i++
					closed = true
					break
				}
				b.WriteByte(c)
				i++
			}
			if !closed {
				return nil, errors.New("DSN 引号未闭合")
			}
			value = b.String()
			if i < n && !isSpace(dsn[i]) {
				return nil, errors.New("DSN 引号后缺空白")
			}
		} else {
			start := i
			for i < n && !isSpace(dsn[i]) {
				i++
			}
			value = dsn[start:i]
		}
		fields = append(fields, keywordField{key: strings.ToLower(key), value: value})
	}
	if len(fields) == 0 {
		return nil, errors.New("DSN 没有字段")
	}
	return fields, nil
}

func isSpace(c byte) bool { return c == ' ' || c == '\t' || c == '\r' || c == '\n' }

// dsnPasswords 返回 DSN 里出现的口令候选（原文与解码后形态）。
// 只用于清洗文本，绝不进入报告。
func dsnPasswords(dsn string) []string {
	set := map[string]bool{}
	if strings.Contains(dsn, "://") {
		if u, err := url.Parse(dsn); err == nil {
			if pw, ok := u.User.Password(); ok && pw != "" {
				set[pw] = true
			}
			if pw := u.Query().Get("password"); pw != "" {
				set[pw] = true
			}
		}
	} else if strings.Contains(dsn, "=") {
		if fields, err := splitKeywordFields(dsn); err == nil {
			for _, f := range fields {
				if f.key == "password" && f.value != "" {
					set[f.value] = true
				}
			}
		}
	}
	out := make([]string, 0, len(set))
	for pw := range set {
		out = append(out, pw)
	}
	return out
}

// sanitizeText 抹掉文本里的 DSN 原文与口令。
// 先整串替换 DSN，再替换口令的原文/查询转义形态，最后抹通用 password= 形状。
func sanitizeText(text, dsn string) string {
	if dsn != "" {
		text = strings.ReplaceAll(text, dsn, "<DSN>")
	}
	for _, pw := range dsnPasswords(dsn) {
		text = strings.ReplaceAll(text, pw, "****")
		if escaped := url.QueryEscape(pw); escaped != pw {
			text = strings.ReplaceAll(text, escaped, "****")
		}
	}
	text = keywordPasswordRE.ReplaceAllString(text, "password=****")
	text = uriUserinfoRE.ReplaceAllString(text, "://****:****@")
	return text
}
