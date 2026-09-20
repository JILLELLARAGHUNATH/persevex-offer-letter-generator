import tempfile
from pathlib import Path
from storage.base import BaseStorage

class LocalStorage(BaseStorage):
    def __init__(self, base_dir: Path = None):
        if base_dir is None:
            self.base_dir = Path(tempfile.gettempdir()) / "persevex_generated"
        else:
            self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_file(self, filename: str, content_bytes: bytes) -> Path:
        target = self.base_dir / Path(filename).name
        target.write_bytes(content_bytes)
        return target

    def get_file_path(self, filename: str) -> Path:
        return self.base_dir / Path(filename).name

    def get_file_url(self, filename: str) -> str:
        safe_name = Path(filename).name
        return f"/generated/{safe_name}"

    def file_exists(self, filename: str) -> bool:
        target = self.base_dir / Path(filename).name
        return target.exists()

    def delete_file(self, filename: str) -> bool:
        target = self.base_dir / Path(filename).name
        if target.exists():
            target.unlink()
            return True
        return False
