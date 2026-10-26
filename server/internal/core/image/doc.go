// Package image 是图片预处理流水线的唯一归属。
//
// # 功能
// Prepare 把宿主拿到的原始图片变成可提交的图片。
// 它解码 JPEG/PNG，按 EXIF 方向摆正，按比例裁切，再重编码。
// 输出只留像素，**不带任何元数据**。
// 本包**不**上传图片、**不**发请求、**不**读写数据库。
// 平台协议只上传与提交，见 internal/signing/contract。
//
// # 归属
// core 层纯领域包。宿主负责旋转、裁剪与重编码。
//
// # 复用
// Options、Output、Prepare。
//
// # 口径
// 图片在交给平台之前完成全部变换。
// 平台侧不再做尺寸或方向修正。
// 无效图片、超限与裁切越界一律返回 errs.CodeValidation。
// 本包不 panic。
package image
