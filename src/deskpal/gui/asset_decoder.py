"""Lazy Qt image adapter; importing this module never imports or starts Qt."""

import struct

from deskpal.assets.models import AssetError, NormalizedImage
from deskpal.assets.service import MAX_EDGE, MAX_FILE_BYTES, MAX_PIXELS


class QtAssetDecoder:
    def _jpeg_single_frame(self, raw: bytes) -> None:
        """Reject MPO/concatenated images rather than decoding just frame zero."""
        position = 2
        scans = False
        while position < len(raw):
            if raw[position] != 0xFF:
                if not scans:
                    raise AssetError("asset_decode_failed")
                marker_at = raw.find(b"\xff", position)
                if marker_at < 0:
                    raise AssetError("asset_decode_failed")
                position = marker_at
            while position < len(raw) and raw[position] == 0xFF:
                position += 1
            if position >= len(raw):
                raise AssetError("asset_decode_failed")
            marker = raw[position]
            position += 1
            if marker == 0 and scans:
                continue  # Entropy byte stuffing.
            if 0xD0 <= marker <= 0xD7 and scans:
                continue  # Restart markers have no segment length.
            if marker == 0xD9:
                if position != len(raw):
                    raise AssetError("asset_animation_unsupported")
                return
            if marker in (0, 0xD8) or position + 2 > len(raw):
                raise AssetError("asset_decode_failed")
            count = struct.unpack_from(">H", raw, position)[0]
            if count < 2 or count > len(raw) - position:
                raise AssetError("asset_decode_failed")
            if marker == 0xE2 and raw[position + 2:position + 6] == b"MPF\0":
                raise AssetError("asset_animation_unsupported")
            scans = marker == 0xDA
            position += count
        raise AssetError("asset_decode_failed")

    def normalize(self, raw: bytes) -> NormalizedImage:
        try:
            from PySide6.QtCore import QByteArray, QBuffer, QIODevice
            from PySide6.QtGui import QImage, QImageReader
        except ImportError:
            raise AssetError("asset_decoder_unavailable") from None
        if not 0 < len(raw) <= MAX_FILE_BYTES:
            raise AssetError("asset_byte_limit")
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            # Reject APNG regardless of whether this Qt build exposes its frames.
            offset = 8
            ended = False
            while offset + 12 <= len(raw):
                count = struct.unpack_from(">I", raw, offset)[0]
                if count > len(raw) - offset - 12:
                    raise AssetError("asset_decode_failed")
                if raw[offset + 4:offset + 8] == b"acTL":
                    raise AssetError("asset_animation_unsupported")
                ended = raw[offset + 4:offset + 8] == b"IEND"
                offset += count + 12
                if ended:
                    break
            if not ended or offset != len(raw):
                raise AssetError("asset_decode_failed")
        elif raw.startswith(b"\xff\xd8"):
            self._jpeg_single_frame(raw)
        else:
            raise AssetError("asset_format_unsupported")
        buffer = QBuffer()
        buffer.setData(QByteArray(raw))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        try:
            reader = QImageReader(buffer)
            reader.setDecideFormatFromContent(True)
            reader.setAutoTransform(True)
            size = reader.size()
            if reader.format().data() not in {b"png", b"jpeg", b"jpg"}:
                raise AssetError("asset_format_unsupported")
            if not size.isValid() or not 1 <= size.width() <= MAX_EDGE or not 1 <= size.height() <= MAX_EDGE or size.width() * size.height() > MAX_PIXELS:
                raise AssetError("asset_dimension_limit")
            if reader.supportsAnimation() or reader.imageCount() not in (0, 1):
                raise AssetError("asset_animation_unsupported")
            image = reader.read()
            if image.isNull() or not 1 <= image.width() <= MAX_EDGE or not 1 <= image.height() <= MAX_EDGE:
                raise AssetError("asset_decode_failed")
            # New storage drops text/EXIF/ICC rather than copying metadata from
            # convertToFormat/copy. Logical scan lines exclude storage padding.
            rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
            fresh = QImage(rgba.width(), rgba.height(), QImage.Format.Format_RGBA8888)
            source = memoryview(rgba.constBits())
            target = memoryview(fresh.bits())
            row_bytes = rgba.width() * 4
            visible = False
            for y in range(rgba.height()):
                start = y * rgba.bytesPerLine()
                row = source[start:start + row_bytes]
                target[y * fresh.bytesPerLine():y * fresh.bytesPerLine() + row_bytes] = row
                visible = visible or any(row[3::4])
            return self._encode(fresh, visible)
        finally:
            buffer.close()

    def _encode(self, image: object, visible: bool) -> NormalizedImage:
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QImage, QImageWriter

        assert isinstance(image, QImage)
        encoded = QByteArray()
        output = QBuffer(encoded)
        output.open(QIODevice.OpenModeFlag.WriteOnly)
        try:
            # QImage.save's format argument differs between PySide runtime and
            # stubs; QImageWriter accepts QByteArray in both.
            if not QImageWriter(output, QByteArray(b"PNG")).write(image):
                raise AssetError("asset_encode_failed")
        finally:
            output.close()
        result = bytes(encoded.data())
        if len(result) > MAX_FILE_BYTES:
            raise AssetError("asset_byte_limit")
        return NormalizedImage(result, image.width(), image.height(), visible)

    def thumbnail(self, png: bytes, max_edge: int) -> NormalizedImage:
        normalized = self.normalize(png)
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage

        # _encode emits only PNG; omit the runtime/stub-incompatible format arg.
        image = QImage.fromData(normalized.png)
        if max(image.width(), image.height()) > max_edge:
            image = image.scaled(max_edge, max_edge, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return self._encode(image, normalized.has_visible_pixels)
