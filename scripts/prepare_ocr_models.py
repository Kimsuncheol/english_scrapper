"""Explicit setup/build command; never invoked by application startup or requests."""

import argparse
from hashlib import sha256
from pathlib import Path
import sys
import ssl
from urllib.request import urlopen

import certifi

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.ocr_models import MODELS, verify_models


def prepare(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    for filename, (url, expected) in MODELS.items():
        target = directory / filename
        if target.is_file() and sha256(target.read_bytes()).hexdigest() == expected:
            continue
        temporary = target.with_suffix(".download")
        try:
            digest, size = sha256(), 0
            with urlopen(url, timeout=60, context=ssl.create_default_context(cafile=certifi.where())) as response, temporary.open("wb") as output:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise RuntimeError("OCR model exceeds the build download limit")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError("Downloaded OCR model checksum mismatch")
            temporary.replace(target)
            print(f"Prepared {filename}: {size} bytes")
        finally:
            temporary.unlink(missing_ok=True)
    verify_models(directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".ocr-models/rapidocr"))
    prepare(parser.parse_args().directory)
