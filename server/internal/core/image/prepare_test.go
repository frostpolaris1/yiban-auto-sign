package image

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"hash/crc32"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// whiteImage 生成 w×h 的纯白图。
func whiteImage(w, h int) *image.NRGBA {
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			img.SetNRGBA(x, y, color.NRGBA{R: 255, G: 255, B: 255, A: 255})
		}
	}
	return img
}

// markedImage 在左上角画 4×2 黑块，其余纯白。
// 黑块位置随方向变换移动，故测试能判定旋转方向而不只看宽高互换。
func markedImage(w, h int) *image.NRGBA {
	img := whiteImage(w, h)
	for y := 0; y < 2; y++ {
		for x := 0; x < 4; x++ {
			img.SetNRGBA(x, y, color.NRGBA{R: 0, G: 0, B: 0, A: 255})
		}
	}
	return img
}

// exifTIFF 手写最小 TIFF 结构：头 + 一个 Orientation 条目。
func exifTIFF(orientation uint16) []byte {
	tiff := []byte{
		'I', 'I', 0x2A, 0x00, // 小端字节序 + 魔数
		0x08, 0x00, 0x00, 0x00, // IFD0 偏移 = 8
		0x01, 0x00, // 条目数 = 1
		0x12, 0x01, // tag = 0x0112 Orientation
		0x03, 0x00, // 类型 = SHORT
		0x01, 0x00, 0x00, 0x00, // 个数 = 1
		0x00, 0x00, 0x00, 0x00, // 值，下面填入
		0x00, 0x00, 0x00, 0x00, // 下一个 IFD = 0
	}
	binary.LittleEndian.PutUint16(tiff[18:20], orientation)
	return tiff
}

// exifAPP1 返回含段长前缀的完整 APP1 段。
func exifAPP1(tiff []byte) []byte {
	payload := append([]byte("Exif\x00\x00"), tiff...)
	seg := []byte{0xFF, 0xE1, 0, 0}
	binary.BigEndian.PutUint16(seg[2:4], uint16(len(payload)+2))
	return append(seg, payload...)
}

// jpegWithAPP1 编码 JPEG 并注入任意 TIFF 载荷的 APP1 段。
// 注入后自检前缀存在；否则「输出无 EXIF」的断言会空转。
func jpegWithAPP1(t *testing.T, src image.Image, tiff []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, src, nil); err != nil {
		t.Fatalf("encode jpeg: %v", err)
	}
	raw := buf.Bytes()
	out := make([]byte, 0, len(raw)+64)
	out = append(out, raw[:2]...)
	out = append(out, exifAPP1(tiff)...)
	out = append(out, raw[2:]...)
	if !bytes.Contains(out, []byte{0xFF, 0xE1}) {
		t.Fatal("注入的 APP1 段缺失：测试前提不成立")
	}
	if !bytes.Contains(out, []byte("Exif\x00\x00")) {
		t.Fatal("注入的 EXIF 标识缺失：测试前提不成立")
	}
	return out
}

// jpegWithOrientation 注入只含方向条目的合法 APP1。
func jpegWithOrientation(t *testing.T, src image.Image, orientation uint16) []byte {
	t.Helper()
	return jpegWithAPP1(t, src, exifTIFF(orientation))
}

// pngChunk 组装一个 PNG 块：长度 + 类型 + 数据 + CRC。
func pngChunk(name string, data []byte) []byte {
	b := make([]byte, 0, 12+len(data))
	var n [4]byte
	binary.BigEndian.PutUint32(n[:], uint32(len(data)))
	b = append(b, n[:]...)
	b = append(b, name...)
	b = append(b, data...)
	crc := crc32.NewIEEE()
	crc.Write([]byte(name))
	crc.Write(data)
	binary.BigEndian.PutUint32(n[:], crc.Sum32())
	return append(b, n[:]...)
}

// pngWithEXIF 在 IHDR 之后插入任意 TIFF 载荷的 eXIf 块。
// IHDR 是首块，长度固定：8 字节签名 + 25 字节块。
func pngWithEXIF(t *testing.T, src image.Image, tiff []byte) []byte {
	t.Helper()
	raw := pngBytes(t, src)
	const ihdrEnd = 8 + 25
	out := make([]byte, 0, len(raw)+len(tiff)+32)
	out = append(out, raw[:ihdrEnd]...)
	out = append(out, pngChunk("eXIf", tiff)...)
	out = append(out, raw[ihdrEnd:]...)
	if !bytes.Contains(out, []byte("eXIf")) {
		t.Fatal("注入的 eXIf 块缺失：测试前提不成立")
	}
	return out
}

// pngWithOrientation 注入只含方向条目的合法 eXIf。
func pngWithOrientation(t *testing.T, src image.Image, orientation uint16) []byte {
	t.Helper()
	return pngWithEXIF(t, src, exifTIFF(orientation))
}

// pngBytes 编码小 PNG，供无效输入与格式用例使用。
func pngBytes(t *testing.T, src image.Image) []byte {
	t.Helper()
	var buf bytes.Buffer
	if err := png.Encode(&buf, src); err != nil {
		t.Fatalf("encode png: %v", err)
	}
	return buf.Bytes()
}

// decoded 解码输出，并核对宽高与 Output 自报值一致。
func decoded(t *testing.T, out Output) image.Image {
	t.Helper()
	img, _, err := image.Decode(bytes.NewReader(out.Data))
	if err != nil {
		t.Fatalf("输出不可解码: %v", err)
	}
	b := img.Bounds()
	if out.Width != b.Dx() || out.Height != b.Dy() {
		t.Fatalf("Output 宽高 %dx%d，实际解码 %dx%d", out.Width, out.Height, b.Dx(), b.Dy())
	}
	return img
}

// luma 返回矩形区域的亮度均值。
// 均值抵消 JPEG 量化噪声，比单点断言稳。
func luma(img image.Image, r image.Rectangle) float64 {
	var sum float64
	n := 0
	for y := r.Min.Y; y < r.Max.Y; y++ {
		for x := r.Min.X; x < r.Max.X; x++ {
			l, _, _, _ := img.At(x, y).RGBA()
			sum += float64(l >> 8)
			n++
		}
	}
	return sum / float64(n)
}

// wantValidation 断言错误码是 validation_failed。
func wantValidation(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("want validation error, got nil")
	}
	if code := errs.From(err).Code; code != errs.CodeValidation {
		t.Fatalf("error code = %q, want %q", code, errs.CodeValidation)
	}
}

// 同一输入与选项必须得到同一字节与同一摘要。
// 重编码路径若不稳，下游按 SHA256 去重会失效。
func TestPrepareJPEGDeterministic(t *testing.T) {
	src := jpegWithOrientation(t, whiteImage(60, 40), 1)
	first, err := Prepare(src, Options{})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}
	second, err := Prepare(src, Options{})
	if err != nil {
		t.Fatalf("Prepare second: %v", err)
	}
	if first.Mime != "image/jpeg" {
		t.Fatalf("Mime = %q, want image/jpeg", first.Mime)
	}
	if first.Width != 60 || first.Height != 40 {
		t.Fatalf("宽高 = %dx%d, want 60x40", first.Width, first.Height)
	}
	if !bytes.Equal(first.Data, second.Data) || first.SHA256 != second.SHA256 {
		t.Fatal("同输入两次输出不一致")
	}
	if first.SHA256 != sha256.Sum256(first.Data) {
		t.Fatal("SHA256 不是输出数据的摘要")
	}
	decoded(t, first)
}

// 输出不得保留 APP1 段或 EXIF 标识。
// 元数据含拍摄时间、设备与位置，带出去就是隐私泄漏。
func TestPrepareStripsMetadata(t *testing.T) {
	src := jpegWithOrientation(t, whiteImage(60, 40), 1)
	out, err := Prepare(src, Options{})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}
	if bytes.Contains(out.Data, []byte{0xFF, 0xE1}) {
		t.Fatal("输出仍含 APP1 段")
	}
	if bytes.Contains(out.Data, []byte("Exif\x00\x00")) {
		t.Fatal("输出仍含 EXIF 标识")
	}
}

// PNG 的 eXIf 块同样可带 GPS，也必须被重编码剥掉。
// 方向为 1 时最容易走「原样直通」捷径，故专门钉住。
func TestPrepareStripsPNGMetadata(t *testing.T) {
	src := pngWithOrientation(t, whiteImage(60, 40), 1)
	out, err := Prepare(src, Options{})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}
	if bytes.Contains(out.Data, []byte("eXIf")) {
		t.Fatal("输出仍含 eXIf 块")
	}
	if bytes.Contains(out.Data, []byte("Exif\x00\x00")) {
		t.Fatal("输出仍含 EXIF 标识")
	}
	decoded(t, out)
}

// EXIF 方向必须落到像素上：宽高互换且标记块移到对应角。
func TestPrepareOrientation(t *testing.T) {
	const w, h = 16, 8
	src := markedImage(w, h)
	cases := []struct {
		name        string
		orientation uint16
		wantW       int
		wantH       int
		dark        image.Rectangle
		light       image.Rectangle
	}{
		{"orientation 1 原样", 1, w, h, image.Rect(0, 0, 4, 2), image.Rect(12, 6, 16, 8)},
		{"orientation 2 水平镜像", 2, w, h, image.Rect(12, 0, 16, 2), image.Rect(0, 0, 4, 2)},
		{"orientation 3 旋转 180", 3, w, h, image.Rect(12, 6, 16, 8), image.Rect(0, 0, 4, 2)},
		{"orientation 4 垂直镜像", 4, w, h, image.Rect(0, 6, 4, 8), image.Rect(0, 0, 4, 2)},
		{"orientation 5 主对角转置", 5, h, w, image.Rect(0, 0, 2, 4), image.Rect(6, 12, 8, 16)},
		{"orientation 6 顺时针 90", 6, h, w, image.Rect(6, 0, 8, 4), image.Rect(0, 12, 2, 16)},
		{"orientation 7 副对角转置", 7, h, w, image.Rect(6, 12, 8, 16), image.Rect(0, 0, 2, 4)},
		{"orientation 8 逆时针 90", 8, h, w, image.Rect(0, 12, 2, 16), image.Rect(6, 0, 8, 4)},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			out, err := Prepare(jpegWithOrientation(t, src, c.orientation), Options{})
			if err != nil {
				t.Fatalf("Prepare: %v", err)
			}
			if out.Width != c.wantW || out.Height != c.wantH {
				t.Fatalf("宽高 = %dx%d, want %dx%d", out.Width, out.Height, c.wantW, c.wantH)
			}
			img := decoded(t, out)
			if got := luma(img, c.dark); got > 90 {
				t.Fatalf("标记块位置亮度 = %.0f, want ≤90（方向不对）", got)
			}
			if got := luma(img, c.light); got < 165 {
				t.Fatalf("对角区块亮度 = %.0f, want ≥165（方向不对）", got)
			}
		})
	}
}

// PNG 的 eXIf 块同样携带方向。
func TestPreparePNGOrientation(t *testing.T) {
	out, err := Prepare(pngWithOrientation(t, markedImage(16, 8), 6), Options{})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}
	if out.Width != 8 || out.Height != 16 {
		t.Fatalf("宽高 = %dx%d, want 8x16", out.Width, out.Height)
	}
	img := decoded(t, out)
	if got := luma(img, image.Rect(6, 0, 8, 4)); got > 90 {
		t.Fatalf("标记块位置亮度 = %.0f, want ≤90（方向不对）", got)
	}
}

// 畸形 EXIF 不得 panic，也不得改变图像。
// 平台上传的图来自任意来源，方向段损坏是常态而非例外。
// 每个载荷同时走 JPEG 与 PNG 两条解析路径。
func TestPrepareToleratesMalformedExif(t *testing.T) {
	cases := []struct {
		name string
		tiff []byte
	}{
		{"非 II/MM 字节序", []byte("XY\x2A\x00\x08\x00\x00\x00\x01\x00")},
		{"魔数错误", []byte{'I', 'I', 0x2B, 0x00, 0x08, 0x00, 0x00, 0x00}},
		{"IFD 偏移越界", []byte{'I', 'I', 0x2A, 0x00, 0x64, 0x00, 0x00, 0x00}},
		{"条目不完整", []byte{'I', 'I', 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00,
			0x01, 0x00, 0x12, 0x01, 0x03, 0x00}},
		{"条目数超过数据长度", []byte{'I', 'I', 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00,
			0xFF, 0xFF}},
		{"不足 8 字节", []byte{'I', 'I', 0x2A, 0x00}},
	}
	const w, h = 16, 8
	src := markedImage(w, h)
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			jpegOut, err := Prepare(jpegWithAPP1(t, src, c.tiff), Options{})
			if err != nil {
				t.Fatalf("JPEG 路径: %v", err)
			}
			pngOut, err := Prepare(pngWithEXIF(t, src, c.tiff), Options{})
			if err != nil {
				t.Fatalf("PNG 路径: %v", err)
			}
			for name, out := range map[string]Output{"jpeg": jpegOut, "png": pngOut} {
				if out.Width != w || out.Height != h {
					t.Fatalf("%s 宽高 = %dx%d, want %dx%d（坏 EXIF 不应改方向）",
						name, out.Width, out.Height, w, h)
				}
				if got := luma(decoded(t, out), image.Rect(0, 0, 4, 2)); got > 90 {
					t.Fatalf("%s 左上标记亮度 = %.0f, want ≤90（方向被误改）", name, got)
				}
			}
		})
	}
}

// TargetMime 为空时保持输入格式；指定时转换。
func TestPrepareTargetMime(t *testing.T) {
	pngSrc := pngBytes(t, whiteImage(20, 10))
	keep, err := Prepare(pngSrc, Options{})
	if err != nil {
		t.Fatalf("Prepare png: %v", err)
	}
	if keep.Mime != "image/png" || !bytes.HasPrefix(keep.Data, []byte("\x89PNG\r\n\x1a\n")) {
		t.Fatalf("PNG 输入应保持 PNG，Mime = %q", keep.Mime)
	}
	decoded(t, keep)

	toJPEG, err := Prepare(pngSrc, Options{TargetMime: "image/jpeg"})
	if err != nil {
		t.Fatalf("Prepare png->jpeg: %v", err)
	}
	if toJPEG.Mime != "image/jpeg" || !bytes.HasPrefix(toJPEG.Data, []byte{0xFF, 0xD8}) {
		t.Fatalf("PNG 转 JPEG 失败，Mime = %q", toJPEG.Mime)
	}
	decoded(t, toJPEG)

	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, whiteImage(20, 10), nil); err != nil {
		t.Fatalf("encode jpeg: %v", err)
	}
	toPNG, err := Prepare(buf.Bytes(), Options{TargetMime: "image/png"})
	if err != nil {
		t.Fatalf("Prepare jpeg->png: %v", err)
	}
	if toPNG.Mime != "image/png" || !bytes.HasPrefix(toPNG.Data, []byte("\x89PNG\r\n\x1a\n")) {
		t.Fatalf("JPEG 转 PNG 失败，Mime = %q", toPNG.Mime)
	}
	decoded(t, toPNG)
}

// 每边裁 5% 后，宽与高各留 90%，容差 1px。
// 同时断言像素确实被搬移：1 像素黑边框裁掉，中心标记保留。
// 只查宽高的用例挡不住「返回同尺寸空图」这类退化。
func TestPrepareCrop(t *testing.T) {
	const w, h = 40, 20
	src := whiteImage(w, h)
	black := color.NRGBA{R: 0, G: 0, B: 0, A: 255}
	for x := 0; x < w; x++ {
		src.SetNRGBA(x, 0, black)
		src.SetNRGBA(x, h-1, black)
	}
	for y := 0; y < h; y++ {
		src.SetNRGBA(0, y, black)
		src.SetNRGBA(w-1, y, black)
	}
	// 中心 4×2 标记，源坐标 (18..21, 9..10)。
	for y := 9; y < 11; y++ {
		for x := 18; x < 22; x++ {
			src.SetNRGBA(x, y, black)
		}
	}
	out, err := Prepare(pngBytes(t, src), Options{CropPercent: 5})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}
	if d := out.Width - w*90/100; d < -1 || d > 1 {
		t.Fatalf("裁切后宽 = %d, want %d±1", out.Width, w*90/100)
	}
	if d := out.Height - h*90/100; d < -1 || d > 1 {
		t.Fatalf("裁切后高 = %d, want %d±1", out.Height, h*90/100)
	}
	img := decoded(t, out)
	// 裁切原点 (2,1)：中心标记落在输出 (16..19, 8..9)。
	if got := luma(img, image.Rect(16, 8, 20, 10)); got > 90 {
		t.Fatalf("中心标记亮度 = %.0f, want ≤90（主体被裁掉）", got)
	}
	if got := luma(img, image.Rect(0, 0, 4, 2)); got < 165 {
		t.Fatalf("输出左上亮度 = %.0f, want ≥165（黑边框未被裁掉）", got)
	}
}

// JPEG 没有 alpha 通道：透明区域必须铺白。
// 不铺白时透明像素编码为黑色，平台按主体判定会直接拒绝。
func TestPrepareFlattensTransparency(t *testing.T) {
	transparent := image.NewNRGBA(image.Rect(0, 0, 20, 20)) // RGB=0, A=0
	out, err := Prepare(pngBytes(t, transparent), Options{TargetMime: "image/jpeg"})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}
	if out.Mime != "image/jpeg" {
		t.Fatalf("Mime = %q, want image/jpeg", out.Mime)
	}
	img := decoded(t, out)
	if got := luma(img, image.Rect(4, 4, 16, 16)); got < 200 {
		t.Fatalf("透明区域亮度 = %.0f, want ≥200（应铺白，不应变黑）", got)
	}
}

// 参数越界必须报 validation，不得 panic。
func TestPrepareRejectsInvalidOptions(t *testing.T) {
	src := pngBytes(t, whiteImage(20, 10))
	cases := []struct {
		name string
		opt  Options
	}{
		{"每边裁切超过 5%", Options{CropPercent: 6}},
		{"每边裁切为负", Options{CropPercent: -1}},
		{"未知输出格式", Options{TargetMime: "image/gif"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := Prepare(src, c.opt)
			wantValidation(t, err)
		})
	}
}

// 非图片、空输入与截断数据必须报 validation，不得 panic。
func TestPrepareRejectsInvalidInput(t *testing.T) {
	big := pngBytes(t, whiteImage(200, 100))
	cases := []struct {
		name string
		data []byte
	}{
		{"空输入", nil},
		{"非图片字节", []byte("not an image at all")},
		{"截断的 PNG", big[:len(big)/2]},
		{"截断的 JPEG", []byte{0xFF, 0xD8, 0xFF}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := Prepare(c.data, Options{})
			wantValidation(t, err)
		})
	}
}

// 超过像素数或单边上限必须报 validation；恰好等于上限必须放行。
func TestPrepareRejectsOversize(t *testing.T) {
	src := pngBytes(t, whiteImage(100, 50))
	cases := []struct {
		name string
		opt  Options
	}{
		{"像素数超过上限", Options{MaxPixels: 100*50 - 1}},
		{"单边超过上限", Options{MaxSidePx: 99}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := Prepare(src, c.opt)
			wantValidation(t, err)
		})
	}
	if _, err := Prepare(src, Options{MaxPixels: 100 * 50, MaxSidePx: 100}); err != nil {
		t.Fatalf("恰好等于上限应放行: %v", err)
	}
}

// 像素上限必须在整图解码之前判定。
// 否则超大图先吃内存再报错，解压炸弹防护失效。
// 载荷：IHDR 声明 2000×2000，IDAT 是坏 zlib——头可读、像素不可解。
func TestPrepareChecksSizeBeforeDecode(t *testing.T) {
	var b bytes.Buffer
	b.WriteString("\x89PNG\r\n\x1a\n")
	ihdr := make([]byte, 13)
	binary.BigEndian.PutUint32(ihdr[0:4], 2000)
	binary.BigEndian.PutUint32(ihdr[4:8], 2000)
	ihdr[8] = 8 // 位深 8
	ihdr[9] = 2 // 真彩色，无 alpha
	b.Write(pngChunk("IHDR", ihdr))
	b.Write(pngChunk("IDAT", []byte("not a zlib stream")))
	b.Write(pngChunk("IEND", nil))
	src := b.Bytes()

	if _, _, err := image.DecodeConfig(bytes.NewReader(src)); err != nil {
		t.Fatalf("测试前提不成立：文件头应可读: %v", err)
	}
	_, err := Prepare(src, Options{MaxPixels: 1000})
	wantValidation(t, err)
	if msg := errs.From(err).Message; !strings.Contains(msg, "pixel count exceeds") {
		t.Fatalf("错误 = %q, want 含 pixel count exceeds（先解码后判上限会先报解码失败）", msg)
	}
}
