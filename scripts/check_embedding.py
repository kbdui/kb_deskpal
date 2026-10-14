"""Opt-in CPU embedding smoke check; first run downloads public model files.

Run from deskpal_env after installing requirements-embedding.txt. This script does not
read application credentials, user memories, or send texts to a cloud model.
"""

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local Chinese BGE embeddings")
    parser.add_argument("--offline", action="store_true", help="Require existing cache; do not download")
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "data" / "models" / "fastembed")
    args = parser.parse_args()
    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError:
        print("Install first: python -m pip install -r requirements-embedding.txt", file=sys.stderr)
        return 1

    model_name = "BAAI/bge-small-zh-v1.5"
    if model_name not in {item["model"] for item in TextEmbedding.list_supported_models()}:
        print("This FastEmbed installation does not list the selected model.", file=sys.stderr)
        return 1
    # Use explicit providers only: providers and cuda/device_ids are alternative
    # constructor options, and must not be specified together.
    model = TextEmbedding(model_name=model_name, cache_dir=str(args.cache_dir.resolve()),
                          threads=4, providers=["CPUExecutionProvider"],
                          local_files_only=args.offline)
    memories = ["用户习惯使用中文交流。", "用户喜欢晚上进行学习。", "用户希望提前收到日程提醒。"]
    instruction = "为这个句子生成表示以用于检索相关文章："
    documents = np.stack(list(model.passage_embed(memories, batch_size=3)))
    query = np.stack(list(model.query_embed([instruction + "用户通常什么时候学习？"], batch_size=1)))
    if (documents.shape != (3, 512) or query.shape != (1, 512)
            or not np.isfinite(documents).all() or not np.isfinite(query).all()):
        raise ValueError("Invalid embedding shape or non-finite values")
    if not (np.allclose(np.linalg.norm(documents, axis=1), 1.0, atol=1e-4)
            and np.allclose(np.linalg.norm(query, axis=1), 1.0, atol=1e-4)):
        raise ValueError("Embeddings are not normalized")
    scores = documents @ query[0]
    print(f"Model: {model_name}; backend: CPU; shape: {documents.shape}; finite/normalized: PASS")
    print("Demo ranking (not a retrieval benchmark):")
    for index in np.argsort(-scores):
        print(f"{scores[index]:.4f}  {memories[index]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
