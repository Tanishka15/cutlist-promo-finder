"""Swappable, single-call providers. Never normalizes the model's raw answer.

``call_model`` writes evidence before inspecting it and returns transport
metadata, not parsed candidates. The pipeline separately parses and validates
``raw_path``. Failed calls are explicit; there are no automatic retries.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request


SCHEMA_PATH = Path(__file__).with_name("candidate_schema.json")


class ModelCallError(RuntimeError):
    """A failed transport/provider call with auditable metadata."""

    def __init__(self, message: str, metadata: dict | None = None):
        super().__init__(message)
        self.metadata = metadata or {}


def _sidecar(raw_path: Path, suffix: str) -> Path:
    return raw_path.with_suffix(suffix)


def call_model(prompt: str, raw_path: Path, *, provider: str,
               model: str | None = None, timeout: int = 240) -> dict:
    """Make one provider request and preserve its exact response on disk.

    Providers: ``codex`` (an already authenticated Codex CLI) and ``openai``
    (Responses API, using OPENAI_API_KEY). ``model`` is required for OpenAI;
    omitting it for Codex preserves the CLI default rather than choosing one.
    Existing answer/evidence files are never overwritten by this function.
    """
    if provider not in {"codex", "openai"}:
        raise ValueError(f"Unsupported provider: {provider!r}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A non-empty prompt is required")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    raw_path = Path(raw_path).resolve()
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_paths = [raw_path, _sidecar(raw_path, ".trace.jsonl"),
                      _sidecar(raw_path, ".stderr.txt"),
                      _sidecar(raw_path, ".http.json")]
    if any(path.exists() for path in evidence_paths):
        raise FileExistsError(f"Evidence already exists for {raw_path.name}; use a new run directory")
    started = time.monotonic()
    metadata = {
        "provider": provider,
        "model": model or "not_reported",
        "model_requested": model,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "raw_response_path": str(raw_path),
        "request_count": 0,
    }
    try:
        if provider == "codex":
            _call_codex(prompt, raw_path, model, timeout, metadata)
        else:
            _call_openai(prompt, raw_path, model, timeout, metadata)
        metadata["status"] = "success"
        return metadata
    except ModelCallError as exc:
        metadata.update(exc.metadata)
        metadata["status"] = "call_failed"
        exc.metadata = metadata
        raise
    except (OSError, ValueError) as exc:
        metadata["status"] = "call_failed"
        raise ModelCallError(str(exc), metadata) from exc
    finally:
        metadata["elapsed_seconds"] = round(time.monotonic() - started, 3)


def _call_codex(prompt, raw_path, model, timeout, metadata):
    executable = shutil.which("codex")
    if not executable:
        raise ModelCallError("codex is not on PATH; install/login to Codex CLI or use --provider openai")
    trace_path = _sidecar(raw_path, ".trace.jsonl")
    stderr_path = _sidecar(raw_path, ".stderr.txt")
    metadata.update(trace_path=str(trace_path), stderr_path=str(stderr_path))
    # An empty working directory avoids loading the screenplays, parent story
    # metadata, or project instructions into the candidate selection context.
    with tempfile.TemporaryDirectory(prefix="model-context-", dir=raw_path.parent) as isolated:
        argv = [executable, "exec", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--ignore-user-config", "--json",
                "--output-schema", str(SCHEMA_PATH), "-o", str(raw_path),
                "-C", isolated]
        if model:
            argv.extend(["--model", model])
        argv.append("-")
        with trace_path.open("xb") as trace, stderr_path.open("xb") as errors:
            metadata["request_count"] = 1
            try:
                result = subprocess.run(argv, input=prompt.encode("utf-8"),
                                        stdout=trace, stderr=errors, timeout=timeout,
                                        check=False)
            except subprocess.TimeoutExpired as exc:
                raise ModelCallError(f"Codex call exceeded {timeout} seconds; evidence retained") from exc
    metadata["returncode"] = result.returncode
    if result.returncode != 0:
        raise ModelCallError(f"Codex exited with status {result.returncode}; see {stderr_path.name} and {trace_path.name}")
    if not raw_path.exists() or not raw_path.stat().st_size:
        raise ModelCallError("Codex completed without a final response; trace retained")
    # Traces and the final answer have already been preserved byte for byte.
    # Detecting unexpected tool activity makes accidental context leaks visible.
    unexpected_tools = []
    for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item", {})
        if item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
            unexpected_tools.append(item.get("type"))
        if event.get("usage"):
            metadata["usage"] = event["usage"]
        if event.get("model"):
            metadata["model"] = event["model"]
    metadata["unexpected_tool_activity"] = unexpected_tools
    if unexpected_tools:
        raise ModelCallError("Selection used unexpected tools; response retained but excluded from valid run")


def _call_openai(prompt, raw_path, model, timeout, metadata):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ModelCallError("OPENAI_API_KEY is not set")
    if not model:
        raise ModelCallError("Choose an available model explicitly with --model for the OpenAI provider")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {
        "model": model,
        "store": False,
        "input": prompt,
        "text": {"format": {"type": "json_schema", "name": "promo_candidates",
                              "strict": True, "schema": schema}},
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    http_path = _sidecar(raw_path, ".http.json")
    metadata["http_response_path"] = str(http_path)
    metadata["request_count"] = 1
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            # Store HTTP evidence before even attempting JSON decoding.
            http_path.write_bytes(body)
            metadata["http_status"] = response.status
            metadata["request_id"] = response.headers.get("x-request-id")
    except urllib.error.HTTPError as exc:
        http_path.write_bytes(exc.read())
        metadata["http_status"] = exc.code
        raise ModelCallError(f"OpenAI HTTP {exc.code}; response retained in {http_path.name}") from exc
    except urllib.error.URLError as exc:
        raise ModelCallError(f"OpenAI connection failed: {exc.reason}") from exc
    try:
        response_data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ModelCallError("OpenAI returned a non-JSON HTTP envelope; original body retained") from exc
    metadata["response_id"] = response_data.get("id")
    metadata["model"] = response_data.get("model", model)
    metadata["usage"] = response_data.get("usage")
    texts = [content.get("text", "")
             for item in response_data.get("output", [])
             if item.get("type") == "message"
             for content in item.get("content", [])
             if content.get("type") == "output_text"]
    refusals = [content.get("refusal", "")
                for item in response_data.get("output", [])
                if item.get("type") == "message"
                for content in item.get("content", [])
                if content.get("type") == "refusal"]
    if texts:
        # No strip(), fence removal, JSON parsing, or reserialization of text.
        raw_path.write_bytes("".join(texts).encode("utf-8"))
    if response_data.get("status") != "completed":
        metadata["response_status"] = response_data.get("status")
        raise ModelCallError("OpenAI response did not complete; original body retained")
    if refusals:
        raise ModelCallError("OpenAI refused candidate selection; original body retained")
    if not texts or not raw_path.stat().st_size:
        raise ModelCallError("OpenAI response contained no candidate text; original body retained")
