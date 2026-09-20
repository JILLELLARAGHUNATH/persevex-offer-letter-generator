from abc import ABC, abstractmethod
from pathlib import Path

class BaseStorage(ABC):
    @abstractmethod
    def save_file(self, filename: str, content_bytes: bytes) -> Path:
        """Save bytes to storage and return path or identifier."""
        pass

    @abstractmethod
    def get_file_path(self, filename: str) -> Path:
        """Get local path to file if available."""
        pass

    @abstractmethod
    def get_file_url(self, filename: str) -> str:
        """Get publicly accessible or proxy URL to the file."""
        pass

    @abstractmethod
    def file_exists(self, filename: str) -> bool:
        """Check if file exists."""
        pass

    @abstractmethod
    def delete_file(self, filename: str) -> bool:
        """Delete file from storage."""
        pass
