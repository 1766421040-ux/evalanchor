"""Explicit providers; only public message content is sent to a model."""
from __future__ import annotations

import json
import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .contracts import ContractError, loads, read_json, fingerprint


# Only digests computed in this process enter this cache. User JSON cannot
# provide cache entries. Metadata changes invalidate large-file hashes, keeping
# CLI prepare -> runner prepare -> provider validation inexpensive. POSIX ctime
# changes on in-place writes even when mtime is restored. Windows ctime may be
# creation time, so that platform re-hashes rather than trusting this shortcut.
_FILE_DIGEST_CACHE: dict[tuple, str] = {}


def _file_identity(path: Path) -> tuple:
    stat = path.stat()
    return (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _hash_file(path: Path) -> tuple[int, str]:
    before = _file_identity(path)
    digest = _FILE_DIGEST_CACHE.get(before) if os.name == "posix" else None
    if digest is None:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        if _file_identity(path) != before:
            raise ContractError(f"Model file changed during hashing: {path.name}")
        digest = hasher.hexdigest()
        if os.name == "posix":
            if len(_FILE_DIGEST_CACHE) >= 1024:
                _FILE_DIGEST_CACHE.clear()
            _FILE_DIGEST_CACHE[before] = digest
    return before[3], digest


def _validate_safetensors(path: Path):
    """Reject missing/empty/obviously invalid weights without loading tensors.

    Transformers/safetensors performs the complete tensor validation at load.
    This preflight never deserializes pickle or invokes remote model code.
    """
    if not path.is_file():
        raise ContractError(f"Missing safetensors weight file: {path.name}")
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise ContractError(f"Invalid safetensors header: {path.name}")
        length = int.from_bytes(prefix, "little")
        if length < 2 or length > 100 * 1024 * 1024 or length + 8 > path.stat().st_size:
            raise ContractError(f"Invalid safetensors header length: {path.name}")
        try:
            header = loads(stream.read(length))
        except (ValueError, UnicodeError) as exc:
            raise ContractError(f"Invalid safetensors JSON header: {path.name}") from exc
    if not isinstance(header, dict):
        raise ContractError(f"Invalid safetensors header object: {path.name}")
    tensors = [value for name, value in header.items() if name != "__metadata__"]
    if not tensors:
        raise ContractError(f"No tensors in safetensors weight file: {path.name}")
    available = path.stat().st_size - 8 - length
    for tensor in tensors:
        if not isinstance(tensor, dict) or not isinstance(tensor.get("dtype"), str):
            raise ContractError(f"Invalid tensor descriptor: {path.name}")
        shape, offsets = tensor.get("shape"), tensor.get("data_offsets")
        if not isinstance(shape, list) or any(type(n) is not int or n < 0 for n in shape):
            raise ContractError(f"Invalid tensor shape: {path.name}")
        if not isinstance(offsets, list) or len(offsets) != 2 or any(type(n) is not int for n in offsets) or not 0 <= offsets[0] <= offsets[1] <= available:
            raise ContractError(f"Invalid tensor data offsets: {path.name}")


def local_model_manifest(model_path: str | Path) -> dict:
    """Hash the local safetensors checkpoint and tokenizer/configuration inputs.

    Files must remain unchanged during a run. This is content provenance, not a
    signature from a model publisher or a defense against hostile filesystem
    mutation. API aliases have no equivalent local weight verification.
    """
    root = Path(model_path).expanduser().resolve()
    if not root.is_dir() or not (root / "config.json").is_file():
        raise ContractError("transformers requires an existing model directory with config.json")
    if not isinstance(read_json(root / "config.json"), dict):
        raise ContractError("Local model config.json must be an object")
    single, index = root / "model.safetensors", root / "model.safetensors.index.json"
    if single.is_file():
        weights = [single]
    elif index.is_file():
        content = read_json(index)
        mapping = content.get("weight_map") if isinstance(content, dict) else None
        if not isinstance(mapping, dict) or not mapping:
            raise ContractError("Safetensors index requires a non-empty weight_map")
        names = set()
        for tensor, name in mapping.items():
            if not isinstance(tensor, str) or not tensor or not isinstance(name, str):
                raise ContractError("Invalid safetensors weight_map")
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or relative.suffix != ".safetensors":
                raise ContractError("Safetensors shard paths must be relative, without traversal, and end in .safetensors")
            names.add(name)
        weights = [root / name for name in sorted(names)]
    else:
        raise ContractError("No usable safetensors checkpoint: require model.safetensors or its complete index; pickle/bin weights are not supported")
    for path in weights:
        _validate_safetensors(path)
    # Covers tokenizer vocabularies/configs, generation config and chat templates.
    # JSON files are included even if an optional library may not use all of them;
    # conservatively rejecting a changed checkpoint is preferable to mixing runs.
    extensions = {".json", ".model", ".txt", ".jinja", ".tiktoken", ".bpe", ".vocab"}
    files = set(weights)
    files.update(p for p in root.iterdir() if p.is_file() and p.suffix in extensions)
    templates = root / "chat_templates"
    if templates.is_dir():
        files.update(p for p in templates.rglob("*") if p.is_file())
    entries = []
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        size, digest = _hash_file(path)
        entries.append({"path": path.relative_to(root).as_posix(), "size_bytes": size, "sha256": digest})
    payload = {"schema_version": 1, "format": "safetensors", "files": entries}
    return {**payload, "sha256": fingerprint(payload)}


class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_target(config: dict) -> dict:
    allowed = {"id", "provider", "model", "revision", "base_url", "api_key_env", "temperature", "max_tokens", "timeout", "outputs", "outputs_path", "input_usd_per_million", "output_usd_per_million", "model_path", "device", "model_manifest"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ContractError(f"Unknown target fields (secrets must use api_key_env): {set(config) - allowed if isinstance(config, dict) else 'not an object'}")
    if not isinstance(config.get("id"), str) or not config["id"]:
        raise ContractError("Target id required")
    if config.get("provider") not in {"replay", "chat-completions", "openai-responses", "transformers"}:
        raise ContractError("provider must be replay, chat-completions, openai-responses, or transformers")
    for name, low, high in (("temperature", 0, 2), ("timeout", 0.1, 600)):
        value = config.get(name, 0 if name == "temperature" else 60)
        if type(value) not in (int, float) or not low <= value <= high:
            raise ContractError(f"Invalid {name}")
    if type(config.get("max_tokens", 256)) is not int or not 1 <= config.get("max_tokens", 256) <= 32768:
        raise ContractError("max_tokens must be an integer in [1,32768]")
    for name in ("input_usd_per_million", "output_usd_per_million"):
        if name in config and (type(config[name]) not in (float, int) or not 0 <= config[name] < 1e6):
            raise ContractError(f"Invalid {name}")
    if config["provider"] in {"chat-completions", "openai-responses"}:
        url = urllib.parse.urlparse(config.get("base_url", ""))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ContractError("base_url must be HTTP(S), without credentials/query/fragment")
        if not config.get("model"):
            raise ContractError("model required")
    if "model_manifest" in config and config["provider"] != "transformers":
        raise ContractError("model_manifest is only valid for local transformers checkpoints")
    return config


def prepare_target(config: dict, relative_to: Path | None = None):
    config = dict(validate_target(config))
    if config["provider"] == "replay":
        if "outputs_path" in config:
            path = Path(config.pop("outputs_path"))
            if not path.is_absolute() and relative_to:
                path = relative_to / path
            config["outputs"] = read_json(path)
        if not isinstance(config.get("outputs"), dict):
            raise ContractError("Replay requires outputs mapping or outputs_path")
    elif config["provider"] == "transformers":
        value = config.get("model_path")
        if not isinstance(value, str) or not value:
            raise ContractError("transformers requires a local model_path")
        path = Path(value).expanduser()
        if not path.is_absolute() and relative_to is not None:
            path = relative_to / path
        config["model_path"] = str(path.resolve())
        actual = local_model_manifest(config["model_path"])
        if "model_manifest" in config and fingerprint(config["model_manifest"]) != fingerprint(actual):
            raise ContractError("Local model manifest mismatch: checkpoint/tokenizer changed or supplied digest is not genuine")
        config["model_manifest"] = actual
    return config


def public_target(config: dict) -> dict:
    # Environment variable names are configuration; values are never read here.
    if config.get("provider") == "transformers":
        config = prepare_target(config)
    public = {k: v for k, v in config.items() if k != "outputs"}
    if "outputs" in config:
        public["outputs_hash"] = fingerprint(config["outputs"])
    return public


class ReplayProvider:
    def __init__(self, config):
        self.outputs = config["outputs"]

    def generate(self, messages: list[dict], case_id: str, repeat: int) -> dict:
        value = self.outputs.get(f"{case_id}:{repeat}", self.outputs.get(case_id))
        if value is None:
            raise ProviderError(f"No replay output for {case_id}:{repeat}")
        if not isinstance(value, str):
            raise ProviderError("Replay values must be raw output strings")
        return {"output": value, "usage": {}, "cost_usd": 0.0, "response": {"fixture": True}, "request": {"messages": messages}, "cached": False}


class ChatProvider:
    def __init__(self, config):
        self.config = config
        self.url = config["base_url"].rstrip("/") + "/chat/completions"
        self.opener = urllib.request.build_opener(NoRedirect())

    def generate(self, messages, case_id, repeat):
        c = self.config
        payload = {"model": c["model"], "messages": messages, "temperature": c.get("temperature", 0), "max_tokens": c.get("max_tokens", 256), "stream": False}
        headers = {"Content-Type": "application/json"}
        if c.get("api_key_env"):
            key = os.environ.get(c["api_key_env"])
            if not key:
                raise ProviderError(f"Missing API key environment variable: {c['api_key_env']}")
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(self.url, json.dumps(payload).encode(), headers, method="POST")
        try:
            with self.opener.open(request, timeout=c.get("timeout", 60)) as response:
                raw = response.read(10_000_001)
        except urllib.error.HTTPError as exc:
            # Do not retain response error bodies: gateways sometimes echo credentials.
            raise ProviderError(f"HTTP {exc.code}", exc.code == 429 or 500 <= exc.code < 600) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"Transport failure: {type(exc).__name__}", True) from exc
        if len(raw) > 10_000_000:
            raise ProviderError("Response exceeds 10 MB")
        try:
            body = loads(raw)
            content = body["choices"][0]["message"]["content"]
            if content is None and body["choices"][0]["message"].get("tool_calls"):
                raise ProviderError("Native tool_calls require an explicit tool protocol; request JSON tool output in the suite")
            if not isinstance(content, str):
                raise ValueError("content is not a string")
            usage = body.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("Invalid token usage object")
            for field in ("prompt_tokens", "completion_tokens"):
                if field in usage and (type(usage[field]) is not int or usage[field] < 0):
                    raise ValueError("Invalid token usage")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Malformed chat completion response") from exc
        cost = None
        if all(k in c for k in ("input_usd_per_million", "output_usd_per_million")) and all(k in usage for k in ("prompt_tokens", "completion_tokens")):
            cost = (usage["prompt_tokens"] * c["input_usd_per_million"] + usage["completion_tokens"] * c["output_usd_per_million"]) / 1e6
        return {"output": content, "usage": usage, "cost_usd": cost, "request": payload, "response": body, "cached": False}


class TransformersProvider:
    """Optional offline local model. No remote code, downloads, or output repair."""
    def __init__(self, config):
        config = prepare_target(config)
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise ContractError("Install evalanchor[local] for local inference") from exc
        self.torch = torch
        self.config = config
        model_path = config.get("model_path")
        if not model_path or not Path(model_path).is_dir():
            raise ContractError("transformers requires an existing local model_path")
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
        device = config.get("device", "cpu")
        self.model = AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True, trust_remote_code=False, use_safetensors=True, torch_dtype=torch.float32 if device == "cpu" else torch.bfloat16).to(device).eval()
        # Detect ordinary file replacement between manifest binding and loading.
        prepare_target(config)

    def generate(self, messages, case_id, repeat):
        t, model, config = self.tokenizer, self.model, self.config
        prompt = t.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = t(prompt, return_tensors="pt", add_special_tokens=False).to(model.device)
        context = getattr(model.config, "max_position_embeddings", None)
        length = inputs["input_ids"].shape[1]
        if context and length + config.get("max_tokens", 256) > context:
            raise ProviderError("Context limit exceeded; silent truncation is forbidden")
        kwargs = {"max_new_tokens": config.get("max_tokens", 256), "do_sample": config.get("temperature", 0) > 0, "pad_token_id": t.eos_token_id}
        if kwargs["do_sample"]:
            kwargs["temperature"] = config["temperature"]
        with self.torch.inference_mode():
            generated = model.generate(**inputs, **kwargs)
        tokens = generated[0, length:]
        output = t.decode(tokens, skip_special_tokens=True)
        return {"output": output, "usage": {"prompt_tokens": length, "completion_tokens": len(tokens)}, "cost_usd": None, "request": {"messages": messages, "rendered_prompt": prompt}, "response": {"generated_token_ids": tokens.tolist()}, "cached": False}


def make_provider(config):
    if config["provider"] == "openai-responses":
        from .openai_provider import OpenAIResponsesProvider
        return OpenAIResponsesProvider(config)
    return {"replay": ReplayProvider, "chat-completions": ChatProvider, "transformers": TransformersProvider}[config["provider"]](config)
