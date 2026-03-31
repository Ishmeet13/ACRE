"""S3 Client for repo snapshot and model artifact storage."""
from __future__ import annotations

import asyncio
import logging
import os
import tarfile
import tempfile

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class S3Client:
    def __init__(self, bucket: str):
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )

    async def upload_directory(self, local_dir: str, s3_key: str) -> str:
        """Tar+gzip a directory and upload to S3. Returns the S3 URL."""
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # Create tarball
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _create_tarball(local_dir, tmp_path),
            )

            # Upload
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.upload_file(
                    tmp_path,
                    self.bucket,
                    s3_key,
                    ExtraArgs={"ServerSideEncryption": "AES256"},
                ),
            )
            url = f"s3://{self.bucket}/{s3_key}"
            logger.info(f"Uploaded {local_dir} → {url}")
            return url

        except ClientError as e:
            # If bucket doesn't exist in local dev, just log and continue
            if "NoSuchBucket" in str(e) or "InvalidAccessKeyId" in str(e):
                logger.warning(f"S3 upload skipped (local dev): {e}")
                return f"local://{s3_key}"
            raise
        finally:
            os.unlink(tmp_path)

    async def upload_file(self, local_path: str, s3_key: str) -> str:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.upload_file(local_path, self.bucket, s3_key),
            )
            return f"s3://{self.bucket}/{s3_key}"
        except ClientError as e:
            logger.warning(f"S3 upload skipped: {e}")
            return f"local://{s3_key}"

    def get_presigned_url(self, s3_key: str, expires_in: int = 3600) -> str:
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": s3_key},
                ExpiresIn=expires_in,
            )
        except ClientError:
            return ""


def _create_tarball(source_dir: str, output_path: str):
    with tarfile.open(output_path, "w:gz") as tar:
        tar.add(source_dir, arcname=os.path.basename(source_dir))
