"""预下载 ChromaDB 默认 ONNX embedding 模型，避免服务首次启动时阻塞。"""

from __future__ import annotations

import pathlib
import shutil
import tarfile
import urllib.request


MODEL_URL = "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
DESTINATION = pathlib.Path("/root/.cache/chroma/onnx_models/all-MiniLM-L6-v2")


def main() -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    archive = DESTINATION / "onnx.tar.gz"

    with urllib.request.urlopen(MODEL_URL, timeout=60) as response, archive.open("wb") as target:
        shutil.copyfileobj(response, target)

    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(DESTINATION, filter="data")

    archive.unlink()


if __name__ == "__main__":
    main()
