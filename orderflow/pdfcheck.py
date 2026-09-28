"""Bounded child-process PDF structure check. Emit only a page count."""

import io
import logging
import sys

from pypdf import PdfReader

MAX_BYTES = 8 * 1024 * 1024


def main() -> int:
    logger = logging.getLogger("pypdf")
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
        return 2
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            return 2
        count = len(reader.pages)
        if count < 1:
            return 2
    except Exception:
        return 2
    sys.stdout.write(str(count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
