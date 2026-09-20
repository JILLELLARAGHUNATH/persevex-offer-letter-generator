import os
from pathlib import Path
from storage.base import BaseStorage
from storage.local_storage import LocalStorage

class S3Storage(BaseStorage):
    """
    AWS S3 Storage implementation.
    Prepared for future AWS deployment without requiring credentials currently.
    Falls back gracefully to LocalStorage if AWS S3 credentials are not configured.
    """
    def __init__(self, bucket_name: str = None, region: str = "us-east-1"):
        self.bucket_name = bucket_name or os.getenv("AWS_S3_BUCKET", "").strip()
        self.region = region or os.getenv("AWS_REGION", "us-east-1").strip()
        self.local_fallback = LocalStorage()
        self._s3_client = None

    def _get_client(self):
        if self._s3_client is not None:
            return self._s3_client
        access_key = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
        if access_key and secret_key and self.bucket_name:
            try:
                import boto3
                self._s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=self.region,
                )
                return self._s3_client
            except Exception as exc:
                print("AWS S3 INIT WARNING (using local storage):", repr(exc))
        return None

    def save_file(self, filename: str, content_bytes: bytes) -> Path:
        client = self._get_client()
        local_path = self.local_fallback.save_file(filename, content_bytes)
        if client and self.bucket_name:
            try:
                safe_key = f"certificates/{Path(filename).name}"
                client.put_object(
                    Bucket=self.bucket_name,
                    Key=safe_key,
                    Body=content_bytes,
                    ContentType="application/pdf"
                )
            except Exception as exc:
                print("AWS S3 UPLOAD WARNING (saved locally):", repr(exc))
        return local_path

    def get_file_path(self, filename: str) -> Path:
        return self.local_fallback.get_file_path(filename)

    def get_file_url(self, filename: str) -> str:
        safe_name = Path(filename).name
        if self._get_client() and self.bucket_name:
            return f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/certificates/{safe_name}"
        return self.local_fallback.get_file_url(filename)

    def file_exists(self, filename: str) -> bool:
        return self.local_fallback.file_exists(filename)

    def delete_file(self, filename: str) -> bool:
        client = self._get_client()
        if client and self.bucket_name:
            try:
                safe_key = f"certificates/{Path(filename).name}"
                client.delete_object(Bucket=self.bucket_name, Key=safe_key)
            except Exception:
                pass
        return self.local_fallback.delete_file(filename)
