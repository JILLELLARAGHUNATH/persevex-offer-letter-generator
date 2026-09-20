import os
from storage.base import BaseStorage
from storage.local_storage import LocalStorage
from storage.s3_storage import S3Storage

_storage_instance = None

def get_storage() -> BaseStorage:
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    storage_type = os.getenv("STORAGE_TYPE", "local").strip().lower()
    if storage_type == "s3":
        _storage_instance = S3Storage()
    else:
        _storage_instance = LocalStorage()

    return _storage_instance
