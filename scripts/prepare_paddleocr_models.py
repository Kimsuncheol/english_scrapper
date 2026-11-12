"""Download and verify official PaddleOCR ONNX models at setup/build time only."""

import argparse
from hashlib import sha256
from pathlib import Path
import shutil
import ssl
import sys
import tarfile
from tempfile import TemporaryDirectory
from urllib.request import urlopen

import certifi

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.paddle_models import BASE_URL, MODELS, verify_models


def prepare(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, spec in MODELS.items():
        target = directory / name
        if all((target / f).is_file() and sha256((target / f).read_bytes()).hexdigest() == h
               for f, h in spec["files"].items()):
            continue
        with TemporaryDirectory(dir=directory) as temporary:
            staging = Path(temporary)
            archive = staging / "model.tar"
            digest, size = sha256(), 0
            url = f"{BASE_URL}/{name}_onnx_infer.tar"
            with urlopen(url, timeout=60, context=ssl.create_default_context(cafile=certifi.where())) as response, archive.open("wb") as output:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise RuntimeError("PaddleOCR model archive exceeds the build limit")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != spec["archive_sha256"]:
                raise RuntimeError("PaddleOCR archive checksum mismatch")
            extracted = staging / "verified"
            extracted.mkdir()
            with tarfile.open(archive) as bundle:
                # Read only named regular files; never extract archive paths,
                # links, caches or arbitrary upstream files into the image.
                for filename, expected in spec["files"].items():
                    member = bundle.getmember(f"{name}_onnx_infer/{filename}")
                    if not member.isfile() or member.size > 32 * 1024 * 1024:
                        raise RuntimeError("Invalid PaddleOCR archive member")
                    with bundle.extractfile(member) as source:
                        content = source.read()
                    if sha256(content).hexdigest() != expected:
                        raise RuntimeError("PaddleOCR extracted model checksum mismatch")
                    (extracted / filename).write_bytes(content)
            if target.exists():
                shutil.rmtree(target)
            extracted.replace(target)
            print(f"Prepared {name}: {size} archive bytes")
    verify_models(directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".ocr-models/paddleocr"))
    prepare(parser.parse_args().directory)
