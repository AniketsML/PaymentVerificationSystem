"""
Content-addressed blob store for prefetched document images.

The bytes live on a disk volume (S3/MinIO-ready behind the same tiny interface); an index
row in `image_blobs` records size/type/provenance. The key is sha256(bytes), so the SAME
image (resubmissions, retries) is stored once and pairs with the OCR cache.

Reference scheme: a job carries `blob:<sha256>` as its image source; pipeline/image_source
resolves that back to bytes. Fetching at INGEST time — while the signed URL is still valid —
is what structurally removes the expired-link failure class.

Writes are atomic (temp file + os.replace) and idempotent (same content = same path). The
store is the single source of truth for the bytes; the DB index is a convenience/audit +
the retention driver, and is self-healing (a missing file is treated as absent).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Optional

from config import settings
from db import pg

BLOB_PREFIX = "blob:"


def is_blob_ref(src: str) -> bool:
    return isinstance(src, str) and src.startswith(BLOB_PREFIX)


def ref_to_sha(src: str) -> str:
    return src[len(BLOB_PREFIX):] if is_blob_ref(src) else ""


class FilesystemBlobStore:
    """Bytes on a local volume, sharded two levels deep to keep directories small."""

    def __init__(self, root: Optional[str] = None):
        self.root = root or settings.IMAGE_STORE_PATH
        os.makedirs(self.root, exist_ok=True)

    # ── paths ─────────────────────────────────────────────────────────────────
    def _path(self, sha: str) -> str:
        return os.path.join(self.root, sha[:2], sha[2:4], sha)

    # ── writes ────────────────────────────────────────────────────────────────
    def put(self, data: bytes, content_type: str = "", source_url: str = "") -> str:
        """Store bytes, return the sha256 key. Idempotent: identical content is a no-op
        on disk and refreshes the index's last_used."""
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)               # atomic publish
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        self._index(sha, len(data), content_type, source_url)
        return sha

    def _index(self, sha: str, size: int, content_type: str, source_url: str) -> None:
        try:
            with pg.pool().connection() as c:
                c.execute(
                    "INSERT INTO image_blobs(sha256,bytes,content_type,source_url) "
                    "VALUES(%s,%s,%s,%s) ON CONFLICT (sha256) DO UPDATE SET last_used=now()",
                    (sha, size, content_type or None, (source_url or None)))
        except Exception:
            pass                                    # index is convenience; the bytes are the truth

    # ── reads ─────────────────────────────────────────────────────────────────
    def get(self, sha_or_ref: str) -> Optional[bytes]:
        sha = ref_to_sha(sha_or_ref) if is_blob_ref(sha_or_ref) else sha_or_ref
        if not sha:
            return None
        path = self._path(sha)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return None                             # self-healing: caller re-fetches / classifies
        except OSError:
            return None
        try:
            with pg.pool().connection() as c:
                c.execute("UPDATE image_blobs SET last_used=now() WHERE sha256=%s", (sha,))
        except Exception:
            pass
        return data

    def exists(self, sha: str) -> bool:
        return os.path.exists(self._path(sha))

    # ── retention ─────────────────────────────────────────────────────────────
    def purge_expired(self, days: int) -> int:
        """Delete blobs unused for `days`, EXCEPT any still referenced by an open job
        (pending/in_progress) — a queued lead must never lose its image. 0 disables."""
        if not days or days <= 0:
            return 0
        with pg.pool().connection() as c:
            rows = c.execute(
                "SELECT sha256 FROM image_blobs "
                "WHERE last_used < now() - %s * interval '1 day' "
                "AND sha256 NOT IN ("
                "  SELECT substring(image_url from %s) FROM jobs "
                "  WHERE status IN ('pending','in_progress') AND image_url LIKE %s)",
                (float(days), len(BLOB_PREFIX) + 1, BLOB_PREFIX + "%")).fetchall()
            removed = 0
            for r in rows:
                sha = r["sha256"]
                path = self._path(sha)
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    continue
                c.execute("DELETE FROM image_blobs WHERE sha256=%s", (sha,))
                removed += 1
        return removed


_store: Optional[FilesystemBlobStore] = None


def store() -> FilesystemBlobStore:
    global _store
    if _store is None:
        _store = FilesystemBlobStore()
    return _store
