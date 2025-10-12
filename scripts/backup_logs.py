#!/usr/bin/env python3
"""Utility to archive logs/ directory and optionally push to S3."""

import argparse
import datetime as dt
import os
import tarfile
from pathlib import Path
from typing import Optional


def create_archive(logs_dir: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    archive_path = backups_dir / f"logs-{timestamp}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(logs_dir, arcname=logs_dir.name)
    return archive_path


def upload_to_s3(archive_path: Path, bucket: str, prefix: Optional[str]) -> None:
    try:
        import boto3  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "boto3 no está instalado. Añade 'boto3' a tus dependencias para subir a S3."
        ) from exc

    client = boto3.client("s3")
    key = f"{prefix.rstrip('/')}/{archive_path.name}" if prefix else archive_path.name
    client.upload_file(str(archive_path), bucket, key)
    print(f"Copia subida a s3://{bucket}/{key}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Respaldar logs del bot en un archivo comprimido")
    parser.add_argument("--logs-dir", default="logs", help="Directorio con archivos a respaldar")
    parser.add_argument("--backups-dir", default="backups", help="Directorio local donde dejar los respaldos")
    parser.add_argument("--s3-bucket", help="Bucket S3 opcional para subir el respaldo", default=os.getenv("S3_BUCKET"))
    parser.add_argument("--s3-prefix", help="Prefijo en S3 para los objetos", default=os.getenv("S3_PREFIX"))
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.exists():
        raise SystemExit(f"No existe el directorio de logs: {logs_dir}")

    archive_path = create_archive(logs_dir, Path(args.backups_dir))
    print(f"Respaldo local creado: {archive_path}")

    if args.s3_bucket:
        upload_to_s3(archive_path, args.s3_bucket, args.s3_prefix)


if __name__ == "__main__":
    main()
