package image

import (
	"bytes"
	"crypto/sha256"
	"image"
	"image/draw"
	"image/jpeg"
	"image/png"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

const (
	mimeJPEG = "image/jpeg"
	mimePNG  = "image/png"

	// cropPercentMax 是每边裁切百分比上限。
	cropPercentMax = 5

	// jpegQuality 是重编码质量。90 保留主体细节，体积可控。
	jpegQuality = 90
)

// Options 是预处理参数。零值表示不限尺寸、不裁切、保持输入格式。
type Options struct {
	// MaxPixels 是像素总数上限。非正值表示不限。
	MaxPixels int

	// MaxSidePx 是单边像素上限。非正值表示不限。
	MaxSidePx int

	// CropPercent 是每边裁切百分比，取值 0..5。0 表示不裁切。
	CropPercent int

	// TargetMime 是输出格式。空串表示保持输入格式。
	// 只接受 "image/jpeg" 与 "image/png"。
	TargetMime string
}

// Output 是预处理结果。
// Data 是最终字节。SHA256 是 Data 的摘要，供去重与审计引用。
type Output struct {
	Data   []byte
	Mime   string
	Width  int
	Height int
	SHA256 [32]byte
}

// Prepare 解码、方向归一、裁切并重编码一张图片。
//
// # 为什么重编码能去元数据
// JPEG 与 PNG 编码器只遍历像素，重新生成文件结构。
// 原始 EXIF、XMP、IPTC、GPS 与缩略图都不会被复制。
// 因此输出不带任何元数据段，无需逐段删除。
//
// # 为什么方向归一必须在宿主侧完成
// signing/contract.ImageRef 只接收预处理后的图。
// 提供方只上传与提交，不做旋转。
// 重编码又会剥掉 EXIF，故方向必须在剥离前写进像素。
//
// # 失败口径
// 无效图片、超出上限、裁切参数越界都返回 errs.CodeValidation。
// 本函数不 panic。
func Prepare(input []byte, opt Options) (Output, error) {
	if opt.CropPercent < 0 || opt.CropPercent > cropPercentMax {
		return Output{}, errs.New(errs.CodeValidation, "crop percent must be between 0 and 5")
	}
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
	if opt.CropPercent > 0 {
		img = crop(img, opt.CropPercent)
	}

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
	}, nil
}

// crop 按每边百分比裁切，返回值原点在 (0,0)。
//
// # 为什么上限是 5%
// 裁切只用于去掉边缘水印与黑边。
// 每边超过 5% 就可能切掉主体，而平台按主体判定通过。
// 每边 5% 是名义上限；实际按整像素四舍五入：
// cut = (边长×5+50)/100。只有边长 mod 20 ≤ 9 时保留 ≥90% 边长
// （20→90%，30→86.67%，50→88%）。边长 ≤9 时 cut=0，实际不裁。
// 面积比例 = 宽保留比 × 高保留比，逐图计算。
// 只有两边都 ≡ 0 (mod 20) 时，面积比例才等于 0.81。
// 调用方按此口径挑图，本函数不设最小边长。
// 上限在 Options 校验处拒绝，本函数不再判越界。
func crop(src image.Image, percent int) image.Image {
	b := src.Bounds()
	cutX := (b.Dx()*percent + 50) / 100 // 四舍五入到像素
	cutY := (b.Dy()*percent + 50) / 100
	r := image.Rect(b.Min.X+cutX, b.Min.Y+cutY, b.Max.X-cutX, b.Max.Y-cutY)
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
