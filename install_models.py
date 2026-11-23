"""Download public model weights once. This command does not read PDF files."""
import hashlib
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from huggingface_hub import snapshot_download

from scan_ocr import DETECTION, RECOGNITION

ROOT = Path(__file__).resolve().parent / "models"
PPOCR_BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5"
OCR_WEIGHTS = (
    ("det", DETECTION, "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae"),
    ("rec", RECOGNITION, "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b"),
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install_ocr():
    target = ROOT / "ppocr"
    target.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target) as work:
        for stage, name, checksum in OCR_WEIGHTS:
            path = target / name
            if path.is_file() and digest(path) == checksum:
                continue
            partial = Path(work) / name
            with urlopen(f"{PPOCR_BASE}/{stage}/{name}", timeout=60) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output)
            if digest(partial) != checksum:
                raise RuntimeError(f"PP-OCRv5 가중치의 SHA-256이 일치하지 않습니다: {name}")
            partial.replace(path)


if __name__ == "__main__":
    for stage in ("detection", "structure-recognition-v1.1-all"):
        snapshot_download(
            f"microsoft/table-transformer-{stage}",
            allow_patterns=["config.json", "model.safetensors"],
            local_dir=ROOT / stage,
        )
    install_ocr()
