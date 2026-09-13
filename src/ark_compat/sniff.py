"""按 magic bytes 嗅探真实媒体类型 —— **绝不信任扩展名或 Content-Type**。

真实案例：一个以 `.jpg` 结尾、Content-Type 写 `image/jpg`（不是合法 MIME）的 URL，
返回的字节其实是 PNG。站点按 Content-Type 校验，直接回 `Unsupported upload content type`。

上传上限按类型分档（实测预签名响应里的 `maxBytes`）：

| 类型 | 上限 |
| --- | --- |
| 图片 | 10 MB |
| 视频 | 50 MB |
| 音频 | 15 MB |

纯函数，零依赖，可单测。
"""

from __future__ import annotations

import struct


def sniff_file(buf: bytes) -> dict[str, str]:
    """返回 `{"content_type", "ext", "kind"}`，kind ∈ image|video|audio|unknown。"""
    if buf is None:
        buf = b""
    if len(buf) >= 8 and buf[:4] == b"\x89PNG":
        return {"content_type": "image/png", "ext": "png", "kind": "image"}
    if len(buf) >= 3 and buf[0] == 0xFF and buf[1] == 0xD8 and buf[2] == 0xFF:
        return {"content_type": "image/jpeg", "ext": "jpg", "kind": "image"}
    if len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return {"content_type": "image/webp", "ext": "webp", "kind": "image"}
    # ISO base media（MP4 / MOV / M4A）：'ftyp' 在 offset 4，brand 在 offset 8
    if len(buf) >= 12 and buf[4:8] == b"ftyp":
        brand = buf[8:12]
        if brand == b"qt  ":
            return {"content_type": "video/quicktime", "ext": "mov", "kind": "video"}
        if brand[:3] in (b"M4A", b"M4B"):
            return {"content_type": "audio/mp4", "ext": "m4a", "kind": "audio"}
        return {"content_type": "video/mp4", "ext": "mp4", "kind": "video"}
    if len(buf) >= 4 and buf[:4] == b"\x1a\x45\xdf\xa3":
        return {"content_type": "video/webm", "ext": "webm", "kind": "video"}
    if len(buf) >= 3 and buf[:3] == b"ID3":
        return {"content_type": "audio/mpeg", "ext": "mp3", "kind": "audio"}
    if len(buf) >= 2 and buf[0] == 0xFF and (buf[1] & 0xE0) == 0xE0:
        # ADTS AAC（0xFFF1 / 0xFFF9）与 MPEG 音频帧同步（0xFFFB / 0xFFF3 …）要分开
        if (buf[1] & 0xF6) == 0xF0:
            return {"content_type": "audio/aac", "ext": "aac", "kind": "audio"}
        return {"content_type": "audio/mpeg", "ext": "mp3", "kind": "audio"}
    if len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WAVE":
        return {"content_type": "audio/wav", "ext": "wav", "kind": "audio"}
    if len(buf) >= 4 and buf[:4] == b"OggS":
        return {"content_type": "audio/ogg", "ext": "ogg", "kind": "audio"}
    return {"content_type": "application/octet-stream", "ext": "bin", "kind": "unknown"}


def sniff_image(buf: bytes) -> dict[str, str]:
    """图片路径的别名 —— 语义上强调"这里必须是图"。"""
    return sniff_file(buf)


def image_dimensions(buf: bytes) -> tuple[int, int]:
    """PNG / JPEG 的像素尺寸，读不出来就返回 `(0, 0)`。零依赖。"""
    if buf is None:
        buf = b""
    # PNG：IHDR 的宽高在 offset 16 / 20
    if len(buf) > 24 and buf[1:4] == b"PNG":
        width, height = struct.unpack(">II", buf[16:24])
        return int(width), int(height)
    # JPEG：扫描 SOFn 段
    if len(buf) > 3 and buf[0] == 0xFF and buf[1] == 0xD8:
        i = 2
        while i < len(buf) - 9:
            if buf[i] != 0xFF:
                i += 1
                continue
            marker = buf[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(buf[i + 5 : i + 7], "big")
                width = int.from_bytes(buf[i + 7 : i + 9], "big")
                return width, height
            seg_len = int.from_bytes(buf[i + 2 : i + 4], "big")
            if seg_len < 2:
                i += 1
                continue
            i += 2 + seg_len
    return 0, 0
