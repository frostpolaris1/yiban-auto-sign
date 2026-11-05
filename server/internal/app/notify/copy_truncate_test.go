package notify

// 文案截断的边界判据：按字符（rune）截断，不按字节。
//
// 多字节文本按字节切会产出非法 UTF-8，落库即报错（notifications.title/body
// 是 text 列，但非法 UTF-8 会被 PostgreSQL 拒绝）。本文件用中文串钉住
// title ≤ 80、body ≤ 500 的字符口径与 UTF-8 合法性。

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestSanitizeCopyTruncatesByRunes(t *testing.T) {
	// 240 个汉字 = 720 字节；按字节截断会切碎最后一个字符。
	title := strings.Repeat("凭据失效", 60)
	body := strings.Repeat("风控拦截", 300)

	gotTitle, gotBody := sanitizeCopy(title, body)

	if count := utf8.RuneCountInString(gotTitle); count != maxTitleChars {
		t.Fatalf("title 字符数 = %d, want %d", count, maxTitleChars)
	}
	if count := utf8.RuneCountInString(gotBody); count != maxBodyChars {
		t.Fatalf("body 字符数 = %d, want %d", count, maxBodyChars)
	}
	if !utf8.ValidString(gotTitle) {
		t.Fatal("截断后的 title 不是合法 UTF-8")
	}
	if !utf8.ValidString(gotBody) {
		t.Fatal("截断后的 body 不是合法 UTF-8")
	}
	if strings.ContainsRune(gotTitle, utf8.RuneError) ||
		strings.ContainsRune(gotBody, utf8.RuneError) {
		t.Fatal("截断产生了替换字符")
	}
}

// 恰好在上限内的文本原样保留；短文本不做任何改写。
func TestSanitizeCopyKeepsBoundaryText(t *testing.T) {
	title := strings.Repeat("风", maxTitleChars)
	body := strings.Repeat("险", maxBodyChars)
	gotTitle, gotBody := sanitizeCopy(title, body)
	if gotTitle != title || gotBody != body {
		t.Fatalf("边界文本被改写：title %d→%d body %d→%d",
			utf8.RuneCountInString(title), utf8.RuneCountInString(gotTitle),
			utf8.RuneCountInString(body), utf8.RuneCountInString(gotBody))
	}

	shortTitle, shortBody := "账号待审核", "账号 138****0001 已提交审核。"
	gotTitle, gotBody = sanitizeCopy(shortTitle, shortBody)
	if gotTitle != shortTitle || gotBody != shortBody {
		t.Fatalf("短文本被改写：%q / %q", gotTitle, gotBody)
	}
}
