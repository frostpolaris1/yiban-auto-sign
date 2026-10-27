package image

import (
	"bytes"
	"crypto/sha256"
	"image"
	"image/draw"
	"image/jpeg"
	"image/png"
	mrand "math/rand/v2"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

const (
	mimeJPEG = "image/jpeg"
	mimePNG  = "image/png"

	// jpegQuality 是重编码质量。90 保留主体细节，体积可控。
	jpegQuality = 90
)

// Rand 是裁切比例的随机源。生产用 systemRand，测试注入确定性实现。
// 本接口只覆盖 [0,n) 取整数的单一能力：注入面最小化。
type Rand interface {
	// IntN 返回 [0,n) 内的一个整数。
	IntN(n int) int
}

// systemRand 是生产随机源。math/rand/v2 顶层函数自动播种，
// 并发安全，进程内无用户可见种子，不构成新全局配置面。
type systemRand struct{}

func (systemRand) IntN(n int) int { return mrand.IntN(n) }

// CropSides 是四边裁切百分比。每边独立取值，口径见 Prepare。
type CropSides struct {
	Left   int
	Right  int
	Top    int
	Bottom int
}

// cropPercentSpan 是单边比例的取值个数：[0,10] 闭区间共 11 个值。
const cropPercentSpan = 11

// drawCropSides 按固定顺序（左、右、上、下）各取一个独立随机比例。
// 顺序固定是绘制口径：测试脚本按同序断言。
func drawCropSides(r Rand) CropSides {
	return CropSides{
		Left:   r.IntN(cropPercentSpan),
		Right:  r.IntN(cropPercentSpan),
		Top:    r.IntN(cropPercentSpan),
		Bottom: r.IntN(cropPercentSpan),
	}
}

// Options 是预处理参数。零值表示不限尺寸、保持输入格式。
// 裁切比例是固定随机口径，不是配置项：无配置键、无环境变量、
// 无管理员面。Rand 只服务测试注入。
type Options struct {
	// MaxPixels 是像素总数上限。非正值表示不限。
	MaxPixels int

	// MaxSidePx 是单边像素上限。非正值表示不限。
	MaxSidePx int

	// Rand 是裁切比例随机源。nil 用 systemRand。测试注入确定性源。
	Rand Rand

	// TargetMime 是输出格式。空串表示保持输入格式。
	// 只接受 "image/jpeg" 与 "image/png"。
	TargetMime string
}

// Output 是预处理结果。
// Data 是最终字节。SHA256 是 Data 的摘要，供去重与审计引用。
// Crop 是本次实际使用的四边比例，审计按此记录。
type Output struct {
	Data   []byte
	Mime   string
	Width  int
	Height int
	SHA256 [32]byte
	Crop   CropSides
}

// Prepare 解码、方向归一、随机裁切并重编码一张图片。
//
// # 裁切口径（固定，不可调）
// 四边各自独立在 [0%,10%] 均匀随机取比例，单边可为 0。
// 随机裁切破坏按像素内容复用同一张图的匹配；上限 10% 保住主体。
// 每次调用独立抽取；同一用户重复上传同一字节会得到不同裁切，
// 去重判据因此按预处理后字节比较（见 app/photos）。
//
// # 变换顺序
// 解码即剥元数据（编码器只重写像素），随后方向归一，再裁切，
// 最后重编码。裁切在像素域进行，输出不含 EXIF。
//
// # 取整
// cut = 边长×比例/100，向下取整。实际裁切比例恒不超 10%；
// 边长 ≤9 时 cut=0，等于不裁。四边合计至多 20%，不会裁出空图。
//
// # 失败口径
// 无效图片、超出上限都返回 errs.CodeValidation。本函数不 panic。
func Prepare(input []byte, opt Options) (Output, error) {
	if opt.TargetMime != "" && opt.TargetMime != mimeJPEG && opt.TargetMime != mimePNG {
		return Output{}, errs.New(errs.CodeValidation, "target mime must be image/jpeg or image/png")
	}
	if len(input) == 0 {
		return Output{}, errs.New(errs.CodeValidation, "image is empty")
	}

	// 先读文件头再解码：超限的图不解码，避免解压炸弹吃内存。
	cfg, format, err := image.DecodeConfig(bytes.NewReader(input))
	if err != nil || (format != "jpeg" && format != "png") {
		return Output{}, errs.New(errs.CodeValidation, "image is not a valid jpeg or png")
	}
	if opt.MaxPixels > 0 && int64(cfg.Width)*int64(cfg.Height) > int64(opt.MaxPixels) {
		return Output{}, errs.New(errs.CodeValidation, "image pixel count exceeds the limit")
	}
	if opt.MaxSidePx > 0 && (cfg.Width > opt.MaxSidePx || cfg.Height > opt.MaxSidePx) {
		return Output{}, errs.New(errs.CodeValidation, "image side exceeds the limit")
	}

	img, _, err := image.Decode(bytes.NewReader(input))
	if err != nil {
		return Output{}, errs.New(errs.CodeValidation, "image decode failed")
	}
	// 统一到 8 位 RGBA。平台原图是 8 位电话照片；
	// 统一位深可避免同一输入因有无 EXIF 而产出不同精度
	// （16 位输入在这里降位深，属明确口径）。
	img = toNRGBA(img)
	switch format {
	case "jpeg":
		if o := jpegOrientation(input); o != 1 {
			img = orient(img, o)
		}
	case "png":
		if o := pngOrientation(input); o != 1 {
			img = orient(img, o)
		}
	}
	rand := opt.Rand
	if rand == nil {
		rand = systemRand{}
	}
	crops := drawCropSides(rand)
	img = crop(img, crops)

	mime := mimePNG
	if format == "jpeg" {
		mime = mimeJPEG
	}
	if opt.TargetMime != "" {
		mime = opt.TargetMime
	}

	var buf bytes.Buffer
	switch mime {
	case mimeJPEG:
		if err := jpeg.Encode(&buf, flattenOnWhite(img), &jpeg.Options{Quality: jpegQuality}); err != nil {
			return Output{}, errs.New(errs.CodeInternal, "image encode failed")
		}
	default:
		if err := png.Encode(&buf, img); err != nil {
			return Output{}, errs.New(errs.CodeInternal, "image encode failed")
		}
	}

	data := buf.Bytes()
	b := img.Bounds()
	return Output{
		Data:   data,
		Mime:   mime,
		Width:  b.Dx(),
		Height: b.Dy(),
		SHA256: sha256.Sum256(data),
		Crop:   crops,
	}, nil
}

// crop 按四边独立比例裁切，返回值原点在 (0,0)。
// cut = 边长×比例/100，向下取整：实际比例恒不超比例上限。
// 边长 ≤9 时 cut=0，该边等于不裁；比例可为 0，结果相同。
// 本函数不设最小边长：四边合计至多 20%，不会裁出空图。
func crop(src image.Image, c CropSides) image.Image {
	b := src.Bounds()
	cutL := b.Dx() * c.Left / 100
	cutR := b.Dx() * c.Right / 100
	cutT := b.Dy() * c.Top / 100
	cutB := b.Dy() * c.Bottom / 100
	r := image.Rect(b.Min.X+cutL, b.Min.Y+cutT, b.Max.X-cutR, b.Max.Y-cutB)
	dst := image.NewNRGBA(image.Rect(0, 0, r.Dx(), r.Dy()))
	draw.Draw(dst, dst.Bounds(), src, r.Min, draw.Src)
	return dst
}

// toNRGBA 把任意图像统一为 8 位 RGBA。
func toNRGBA(src image.Image) image.Image {
	b := src.Bounds()
	dst := image.NewNRGBA(image.Rect(0, 0, b.Dx(), b.Dy()))
	draw.Draw(dst, dst.Bounds(), src, b.Min, draw.Src)
	return dst
}

// flattenOnWhite 把图铺到白底上。
// JPEG 没有 alpha 通道，透明像素直接编码会变黑。
func flattenOnWhite(src image.Image) image.Image {
	b := src.Bounds()
	dst := image.NewRGBA(image.Rect(0, 0, b.Dx(), b.Dy()))
	draw.Draw(dst, dst.Bounds(), image.White, image.Point{}, draw.Src)
	draw.Draw(dst, dst.Bounds(), src, b.Min, draw.Over)
	return dst
}

// orient 按 EXIF 方向摆正像素，返回值原点在 (0,0)。
// 2/4/5/7 是镜像方向，同一张表一起处理，避免留下未覆盖支路。
func orient(src image.Image, orientation int) image.Image {
	b := src.Bounds()
	w, h := b.Dx(), b.Dy()
	dw, dh := w, h
	if orientation >= 5 {
		dw, dh = h, w // 5..8 交换宽高
	}
	dst := image.NewNRGBA(image.Rect(0, 0, dw, dh))
	for y := 0; y < dh; y++ {
		for x := 0; x < dw; x++ {
			sx, sy := unrotate(orientation, x, y, w, h)
			dst.Set(x, y, src.At(b.Min.X+sx, b.Min.Y+sy))
		}
	}
	return dst
}

// unrotate 把目标坐标映射回源坐标。
func unrotate(orientation, x, y, w, h int) (int, int) {
	switch orientation {
	case 2:
		return w - 1 - x, y
	case 3:
		return w - 1 - x, h - 1 - y
	case 4:
		return x, h - 1 - y
	case 5:
		return y, x
	case 6:
		return y, h - 1 - x
	case 7:
		return w - 1 - y, h - 1 - x
	case 8:
		return w - 1 - y, x
	default:
		return x, y
	}
}
