package image

import (
	"bytes"
	"encoding/binary"
)

// pngSignature 是 PNG 文件头。
var pngSignature = []byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n'}

// pngOrientation 返回 PNG eXIf 块里的方向值，取值 1..8。
// 无 eXIf、结构错、值越界都返回 1（原样）。
func pngOrientation(data []byte) int {
	if len(data) < 8 || !bytes.Equal(data[:8], pngSignature) {
		return 1
	}
	i := 8
	for i+8 <= len(data) {
		n := uint64(binary.BigEndian.Uint32(data[i : i+4]))
		name := string(data[i+4 : i+8])
		if uint64(i)+8+n > uint64(len(data)) {
			return 1
		}
		if name == "eXIf" {
			return tiffOrientation(data[i+8 : i+8+int(n)])
		}
		if name == "IEND" {
			return 1
		}
		i += 12 + int(n)
	}
	return 1
}

// jpegOrientation 返回 JPEG 的 EXIF 方向值，取值 1..8。
// 无 EXIF、结构错、值越界都返回 1（原样）。
// 解析失败不报错：方向缺失不等于图片无效。
func jpegOrientation(data []byte) int {
	if len(data) < 4 || data[0] != 0xFF || data[1] != 0xD8 {
		return 1
	}
	i := 2
	for i+1 < len(data) {
		if data[i] != 0xFF {
			return 1
		}
		marker := data[i+1]
		i += 2
		if marker == 0xFF { // 填充字节：回到当前标记
			i--
			continue
		}
		switch {
		case marker == 0xD8 || marker == 0x01 || (marker >= 0xD0 && marker <= 0xD7):
			continue // 无长度的独立标记
		case marker == 0xD9 || marker == 0xDA:
			return 1 // 文件尾或图像数据：EXIF 只会在更早的段
		}
		if i+2 > len(data) {
			return 1
		}
		n := int(binary.BigEndian.Uint16(data[i : i+2]))
		if n < 2 || i+n > len(data) {
			return 1
		}
		seg := data[i+2 : i+n]
		if marker == 0xE1 && len(seg) >= 6 && string(seg[:4]) == "Exif" && seg[4] == 0 && seg[5] == 0 {
			return tiffOrientation(seg[6:])
		}
		i += n
	}
	return 1
}

// tiffOrientation 从 TIFF 结构里取 Orientation（tag 0x0112）。
// 全部读取都先核边界，坏数据返回 1 而不是 panic。
func tiffOrientation(t []byte) int {
	if len(t) < 8 {
		return 1
	}
	var order binary.ByteOrder
	switch string(t[:2]) {
	case "II":
		order = binary.LittleEndian
	case "MM":
		order = binary.BigEndian
	default:
		return 1
	}
	if order.Uint16(t[2:4]) != 0x002A {
		return 1
	}
	off64 := uint64(order.Uint32(t[4:8]))
	if off64 < 8 || off64+2 > uint64(len(t)) {
		return 1
	}
	off := int(off64)
	count := int(order.Uint16(t[off : off+2]))
	pos := off + 2
	for n := 0; n < count; n++ {
		if pos+12 > len(t) {
			return 1
		}
		entry := t[pos : pos+12]
		if order.Uint16(entry[0:2]) == 0x0112 && order.Uint16(entry[2:4]) == 3 {
			o := int(order.Uint16(entry[8:10]))
			if o >= 1 && o <= 8 {
				return o
			}
			return 1
		}
		pos += 12
	}
	return 1
}
