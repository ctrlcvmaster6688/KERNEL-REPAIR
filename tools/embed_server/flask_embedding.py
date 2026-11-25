#!/usr/bin/env python3
"""BGE-M3 embedding service for CoHiKer++.

Why this file exists
--------------------
Both knowledge-base retrievals are dense: `core/Hybrid_patch_rag.py`
(`search_historical_fixes`) and `skill/kernel_fix_expert.py`
(`query_golden_fixes`) encode the *query* with an HTTP call to a BGE-M3
embedding service.  The original working tree started that service by hand from
a script outside the repository, with the model path, device and port
hard-coded.  This is the packaged version: same protocol, same pooling math,
same truncation length, but everything configurable.

Do NOT change the pooling math or `max_length`
----------------------------------------------
The 21 132 `intent_vec` blobs already stored in `data/golden_kernel_test_v3.db`
(and the `*_vec` columns of the semantic KB) were produced with exactly this
computation: mean pooling over `last_hidden_state` with the attention mask,
L2-normalised, `max_length=1024`, 1024 dimensions.  Changing any of it makes
query vectors incompatible with the stored ones and silently degrades retrieval.

Protocol (unchanged)
--------------------
    POST /embed   {"content": "<text>"}
               -> {"embedding": [1024 floats], "model": "bge-m3", "dim": 1024}
               -> 400 {"error": "Content is empty"} for an empty payload
    GET  /health
               -> {"status": "ok", "model": "bge-m3", "dim": 1024, "device": "cuda:1"}

Configuration (CLI flag overrides the environment variable)
-----------------------------------------------------------
    COHIKER_EMBED_MODEL_PATH   directory with the BGE-M3 weights (required, no default)
    COHIKER_EMBED_PORT         HTTP port (default 6001)
    COHIKER_EMBED_DEVICE       "auto" (default) -> cuda:1 when >=2 GPUs, cuda:0 when 1,
                               cpu when none; or any explicit torch device string
    COHIKER_EMBED_MAX_LENGTH   token truncation (default 1024, keep it)
    HF_ENDPOINT                HuggingFace mirror used if the weights are fetched
                               (default https://hf-mirror.com)

Model weights are not shipped (2.2 GB).  Fetch them once, then point the
variable above at the directory:

    huggingface-cli download BAAI/bge-m3 --local-dir /path/to/bge-m3
    export COHIKER_EMBED_MODEL_PATH=/path/to/bge-m3

Run:

    python tools/embed_server/flask_embedding.py --model-path /path/to/bge-m3 --port 6001

Requires `flask`, `torch` and `transformers`.  They are imported lazily, so
`--help` works on a machine where they are not installed yet.
"""

from __future__ import annotations

import argparse
import os
import sys

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 6001
DEFAULT_MAX_LENGTH = 1024
EMBED_DIM = 1024
MODEL_NAME = "bge-m3"

MODEL_PATH_HINT = (
    "COHIKER_EMBED_MODEL_PATH is not set.\n"
    "Download the BGE-M3 weights once (HuggingFace 'BAAI/bge-m3', ~2.2 GB) into a\n"
    "local directory and point the variable (or --model-path) at it:\n"
    "  huggingface-cli download BAAI/bge-m3 --local-dir /path/to/bge-m3\n"
    "  export COHIKER_EMBED_MODEL_PATH=/path/to/bge-m3"
)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def resolve_model_path(cli_value: str = None) -> str:
    """Model directory: --model-path > COHIKER_EMBED_MODEL_PATH; must exist."""
    raw = cli_value or os.environ.get("COHIKER_EMBED_MODEL_PATH", "")
    raw = (raw or "").strip()
    if not raw:
        raise SystemExit(MODEL_PATH_HINT)
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        raise SystemExit(
            f"BGE-M3 model directory not found: {path}\n"
            f"(from {'--model-path' if cli_value else 'COHIKER_EMBED_MODEL_PATH'})"
        )
    return path


def resolve_port(cli_value=None) -> int:
    raw = cli_value if cli_value is not None else os.environ.get("COHIKER_EMBED_PORT", DEFAULT_PORT)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"invalid embedding port: {raw!r} (expected an integer)")
    if not (0 < port < 65536):
        raise SystemExit(f"invalid embedding port: {port} (must be 1..65535)")
    return port


def resolve_device(cli_value: str = None) -> str:
    """--device > COHIKER_EMBED_DEVICE > "auto".

    "auto" resolves to cuda:1 when at least two GPUs are visible, cuda:0 when
    exactly one is, and cpu otherwise (the original script always used cuda:1 and
    crashed on single-GPU hosts).
    """
    spec = (cli_value if cli_value is not None else os.environ.get("COHIKER_EMBED_DEVICE", "auto"))
    spec = (spec or "auto").strip() or "auto"
    if spec != "auto":
        return spec

    try:
        import torch
    except ImportError as exc:
        raise SystemExit(f"'torch' is required to auto-detect the device: {exc}")
    if not torch.cuda.is_available():
        return "cpu"
    return "cuda:1" if torch.cuda.device_count() >= 2 else "cuda:0"


def resolve_max_length(cli_value=None) -> int:
    raw = cli_value if cli_value is not None else os.environ.get("COHIKER_EMBED_MAX_LENGTH", DEFAULT_MAX_LENGTH)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"invalid COHIKER_EMBED_MAX_LENGTH: {raw!r}")
    if value <= 0:
        raise SystemExit(f"invalid COHIKER_EMBED_MAX_LENGTH: {value} (must be > 0)")
    return value


# ---------------------------------------------------------------------------
# Model / inference (kept byte-for-byte equivalent to the original pipeline)
# ---------------------------------------------------------------------------
def load_model(model_path: str, device: str):
    """Tokenizer + model, eval mode, moved to ``device``."""
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc:  # pragma: no cover - depends on the host env
        raise SystemExit(
            f"'torch' and 'transformers' are required to serve BGE-M3: {exc}\n"
            f"  pip install torch transformers"
        )

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    print(f"[embed] loading BGE-M3 from {model_path} on {device}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    model = AutoModel.from_pretrained(model_path, trust_remote_code=True).to(torch.device(device))
    model.eval()
    print("[embed] model loaded", flush=True)
    return tokenizer, model


def compute_embedding(text, tokenizer, model, device, max_length=DEFAULT_MAX_LENGTH):
    """Mean-pooled, L2-normalised sentence embedding (1024-d).

    This is the exact computation used to pre-compute the KB vectors; do not
    change the pooling, the normalisation or ``max_length``.
    """
    import torch
    import torch.nn.functional as F

    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=max_length,
        padding=True,
    ).to(torch.device(device))

    with torch.no_grad():
        outputs = model(**inputs)
        token_embeddings = outputs.last_hidden_state
        attention_mask = inputs["attention_mask"]
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        sum_embeddings = torch.sum(token_embeddings * input_mask_expanded, 1)
        sum_mask = torch.clamp(input_mask_expanded.sum(1), min=1e-9)
        embeddings = sum_embeddings / sum_mask
        embeddings = F.normalize(embeddings, p=2, dim=1)

    return embeddings[0].cpu().numpy().tolist()


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def create_app(tokenizer, model, device: str, max_length: int = DEFAULT_MAX_LENGTH):
    try:
        from flask import Flask, jsonify, request
    except ImportError as exc:  # pragma: no cover - depends on the host env
        raise SystemExit(f"'flask' is required to serve the embedding API: {exc}\n  pip install flask")

    app = Flask(__name__)

    @app.route("/embed", methods=["POST"])
    def embed_endpoint():
        payload = request.get_json(silent=True) or {}
        content = payload.get("content", "")
        if not content:
            return jsonify({"error": "Content is empty"}), 400
        try:
            embedding = compute_embedding(content, tokenizer, model, device, max_length)
            return jsonify({"embedding": embedding, "model": MODEL_NAME, "dim": len(embedding)})
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            return jsonify({"error": str(exc)}), 500

    @app.route("/health", methods=["GET"])
    def health_endpoint():
        return jsonify({"status": "ok", "model": MODEL_NAME, "dim": EMBED_DIM, "device": device})

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flask_embedding.py",
        description="Local BGE-M3 embedding service used by the two dense "
                    "knowledge-base retrievals (POST /embed, GET /health).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "environment variables:\n"
            "  COHIKER_EMBED_MODEL_PATH   BGE-M3 weights directory (required)\n"
            "  COHIKER_EMBED_PORT         default 6001\n"
            "  COHIKER_EMBED_DEVICE       auto | cpu | cuda:0 | cuda:1 ... (default auto)\n"
            "  COHIKER_EMBED_MAX_LENGTH   default 1024 (keep it)\n"
            "\n"
            "example:\n"
            "  export COHIKER_EMBED_MODEL_PATH=/path/to/bge-m3\n"
            "  python tools/embed_server/flask_embedding.py --port 6001\n"
        ),
    )
    parser.add_argument("--model-path", default=None,
                        help="BGE-M3 weights directory (overrides COHIKER_EMBED_MODEL_PATH).")
    parser.add_argument("--port", default=None,
                        help="HTTP port (overrides COHIKER_EMBED_PORT, default 6001).")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"bind address (default {DEFAULT_HOST}).")
    parser.add_argument("--device", default=None,
                        help="torch device or 'auto' (overrides COHIKER_EMBED_DEVICE).")
    parser.add_argument("--max-length", default=None, type=int,
                        help="token truncation length (overrides COHIKER_EMBED_MAX_LENGTH, "
                             "default 1024; keep it, the KB vectors were built with 1024).")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    model_path = resolve_model_path(args.model_path)
    port = resolve_port(args.port)
    device = resolve_device(args.device)
    max_length = resolve_max_length(args.max_length)

    print(f"[embed] model_path={model_path}")
    print(f"[embed] device={device} max_length={max_length} port={port} host={args.host}")
    print("[embed] endpoints: POST /embed, GET /health")

    tokenizer, model = load_model(model_path, device)
    app = create_app(tokenizer, model, device, max_length)

    # threaded=False keeps one GPU request at a time, as in the original setup.
    app.run(host=args.host, port=port, threaded=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
