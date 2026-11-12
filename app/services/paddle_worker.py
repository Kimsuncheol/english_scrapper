"""Private subprocess entry point. Stdout contains only the normalized OCR JSON."""

from contextlib import redirect_stdout
import json
import os
import socket
import sys


def blocked(*args, **kwargs):
    raise RuntimeError("Network access is disabled in the PaddleOCR worker")


def main():
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    # Fully local model paths plus blocked sockets prevent fallback downloads.
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.getaddrinfo = blocked
    from app.services.ocr import OCRConfig
    from app.services.paddle_ocr import create_paddleocr_api, decode_image, normalize_paddleocr
    config = OCRConfig(**json.loads(sys.argv[1]))
    # Native imports can write directly to stdout; reserve a separate descriptor
    # for our result and send all dependency output to the discarded stderr pipe.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    with redirect_stdout(sys.stderr):
        try:
            api = create_paddleocr_api(config)
        except Exception:
            return 3
        if "--check" in sys.argv:
            return 0
        try:
            import numpy as np
            with decode_image(sys.stdin.buffer.read(config.max_bytes + 1), config) as image:
                # PaddleOCR expects OpenCV's BGR order for ndarray input.
                predictions = api.predict(np.asarray(image)[:, :, ::-1].copy())
                if len(predictions) != 1:
                    return 4
                result = normalize_paddleocr(predictions[0], image.width, image.height,
                                             config.min_confidence)
        except Exception:
            return 4
    output.write(result.model_dump_json())
    output.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
