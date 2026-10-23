"""Opt-in, cache-only Chinese CPU embeddings; importing this module is cheap."""

import hashlib
import importlib
import importlib.metadata
import math
from pathlib import Path
from typing import Any, Iterable

from .contracts import EmbeddingManifest, MemoryError
from .cache_lock import CacheLease

MODEL = "BAAI/bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
DIMENSIONS = 512


class EmbeddingError(MemoryError):
    """Embedding unavailable or invalid; safe to report without input text."""


def normalized_vector(vector: Iterable[float], dimensions: int) -> tuple[float, ...]:
    """Validate shape/numbers and normalize without numpy or SDK dependencies."""
    try:
        values = tuple(float(value) for value in vector)
        if len(values) != dimensions or not all(math.isfinite(value) for value in values):
            raise ValueError
        norm = math.sqrt(math.fsum(value * value for value in values))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError
        return tuple(value / norm for value in values)
    except (TypeError, ValueError, OverflowError):
        raise EmbeddingError("Embedding vector has invalid dimensions or non-finite values") from None


class FastEmbedProvider:
    """Explicit initialization loads existing FastEmbed 0.9.0 cache only.

    Query prefix is added exactly here, never to documents. Long inputs are
    rejected using the actual model tokenizer rather than silently truncated;
    retrievers can retain complete assertions through lexical fallback. Small
    batches and one CPU session avoid process pools and duplicate model loads.
    """

    def __init__(self, *, cache_dir: str | Path, threads: int = 4) -> None:
        if isinstance(threads, bool) or not isinstance(threads, int) or threads <= 0:
            raise EmbeddingError("Embedding threads must be a positive integer")
        cache = Path(cache_dir).expanduser().resolve()
        if not cache.is_dir():
            raise EmbeddingError("Embedding cache is missing; prepare model files explicitly")
        self._lease = CacheLease(cache, exclusive=False)
        try:
            self._lease.acquire()
            if importlib.metadata.version("fastembed") != "0.9.0":
                raise EmbeddingError("This adapter requires FastEmbed 0.9.0")
            TextEmbedding = importlib.import_module("fastembed").TextEmbedding

            self._model: Any = TextEmbedding(
                model_name=MODEL, cache_dir=str(cache), threads=threads,
                providers=["CPUExecutionProvider"], local_files_only=True,
            )
            root = Path(self._model.model._model_dir)
            files = sorted(path for path in root.rglob("*") if path.is_file())
            if not any(path.suffix == ".onnx" for path in files) or not (root / "tokenizer.json").is_file():
                raise EmbeddingError("Embedding cache lacks model weights or tokenizer")
            digest = hashlib.sha256()
            self.weight_checksums: tuple[tuple[str, str], ...] = tuple(
                (path.relative_to(root).as_posix(), self._file_hash(path)) for path in files
            )
            for name, checksum in self.weight_checksums:
                digest.update((name + "\0" + checksum + "\n").encode("utf-8"))
            # Hash every tokenizer/config/weight asset as well as preprocessing.
            self._manifest = EmbeddingManifest(
                MODEL, DIMENSIONS,
                "fastembed-0.9.0:cpu:assets-sha256=" + digest.hexdigest()
                + ":strip-v1:query-prefix-zh-v1:l2-v1:token-limit-512-reject-v1",
            )
            tokenizer = self._model.model.tokenizer
            self._tokenizer: Any = type(tokenizer).from_str(tokenizer.to_str())
            self._tokenizer.no_truncation()
            self._tokenizer.no_padding()
            self._closed = False
        except EmbeddingError:
            self._lease.release()
            raise
        except Exception:
            self._lease.release()
            raise EmbeddingError("Local embedding initialization failed; no model was downloaded") from None

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    @property
    def manifest(self) -> EmbeddingManifest:
        return self._manifest

    def _text(self, text: str, *, query: bool = False) -> str:
        if self._closed:
            raise EmbeddingError("Embedding provider is closed")
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("Embedding input must be nonempty text")
        prepared = (QUERY_PREFIX if query else "") + text.strip()
        if len(self._tokenizer.encode(prepared).ids) > 512:
            raise EmbeddingError("Embedding input exceeds 512 tokens; complete text was not truncated")
        return prepared

    def embed_query(self, text: str) -> tuple[float, ...]:
        prepared = self._text(text, query=True)
        try:
            vectors = tuple(self._model.query_embed([prepared], batch_size=1))
            if len(vectors) != 1:
                raise EmbeddingError("Embedding query returned an invalid vector count")
            return normalized_vector(vectors[0], DIMENSIONS)
        except Exception:
            raise EmbeddingError("Local query embedding failed") from None

    def embed_documents(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if self._closed:
            raise EmbeddingError("Embedding provider is closed")
        if not isinstance(texts, tuple):
            raise EmbeddingError("Embedding documents must be a tuple of texts")
        output: list[tuple[float, ...]] = []
        for offset in range(0, len(texts), 8):
            prepared = [self._text(text) for text in texts[offset:offset + 8]]
            try:
                vectors = tuple(self._model.passage_embed(prepared, batch_size=8))
                if len(vectors) != len(prepared):
                    raise EmbeddingError("Embedding documents returned an invalid vector count")
                output.extend(normalized_vector(vector, DIMENSIONS) for vector in vectors)
            except Exception:
                raise EmbeddingError("Local document embedding failed") from None
        return tuple(output)

    def close(self) -> None:
        """Release this adapter's CPU session/tokenizer references, idempotently."""
        self._closed = True
        self._model = None
        self._tokenizer = None
        self._lease.release()


__all__ = ["FastEmbedProvider", "EmbeddingError"]
