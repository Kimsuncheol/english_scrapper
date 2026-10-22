"""Offline real-model smoke check; provision models separately before running."""

import argparse
from pathlib import Path
import resource
import socket
import sys
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def blocked(*args, **kwargs):
    raise RuntimeError('Network access is disabled during the OCR smoke check')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', nargs='?', type=Path, default=Path('app/dev_site/assets/lesson.png'))
    args = parser.parse_args()
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.getaddrinfo = blocked
    from app.services.ocr import OCRService
    service = OCRService()
    try:
        start = monotonic()
        if not service.initialize():
            raise RuntimeError('OCR initialization failed; check provisioned models and dependencies')
        print(f'Initialization: {monotonic()-start:.2f}s')
        data = args.image.read_bytes()
        for index in range(2):
            start = monotonic()
            result = service._recognize(data)
            print(f'Pass {index+1}: {monotonic()-start:.2f}s, engine={result.engine}, '
                f'regions={len(result.regions)}, status={result.status}')
            if not result.regions:
                raise RuntimeError('Smoke image produced no readable regions')
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024**2 if sys.platform == 'darwin' else 1024
        print(f'Peak RSS: {peak/divisor:.1f} MiB (local platform; not a Render measurement)')
    finally:
        service.close()


if __name__ == '__main__':
    main()
