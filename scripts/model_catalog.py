"""Lossless native catalog derivation; source freshness is verified by the owner."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from urllib.parse import urlsplit

import owned_files as files
from tomlkit import dumps

MAX_CATALOG_BYTES = 8 * 1024 * 1024
RULE_PATH = Path(__file__).resolve().parents[1] / "templates/codex-default-mode.md.install"
ASSETS = {"source.json", "derived.json", "rule.md", "proof.json"}


class ModelCatalogError(files.OwnershipError):
    pass


def _invalid_constant(_value: str):
    raise ModelCatalogError("模型目录包含非 JSON 数值")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ModelCatalogError("模型目录数值超出有限范围")
    return number


def parse_catalog(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CATALOG_BYTES:
        raise ModelCatalogError("模型目录为空或超过读取上限")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=files.unique_pairs,
                           parse_constant=_invalid_constant, parse_float=_finite_float)
    except (UnicodeError, ValueError, files.OwnershipError) as error:
        raise ModelCatalogError("模型目录不是无重复键的 UTF-8 JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("models"), list) or not value["models"]:
        raise ModelCatalogError("模型目录必须包含非空 models 列表")
    slugs = set()
    for model in value["models"]:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str) or not model["slug"].strip():
            raise ModelCatalogError("模型标识缺失或无效")
        if model["slug"] in slugs:
            raise ModelCatalogError("模型标识重复")
        slugs.add(model["slug"])
        messages = model.get("model_messages")
        if messages is not None:
            if not isinstance(messages, dict):
                raise ModelCatalogError("model_messages 不是对象或 null")
            modes = messages.get("collaboration_modes")
            if modes is not None:
                if not isinstance(modes, dict):
                    raise ModelCatalogError("collaboration_modes 不是对象或 null")
                if modes.get("default") is not None and not isinstance(modes["default"], str):
                    raise ModelCatalogError("Default 模式规则不是字符串或 null")
    return value


def rule_bytes() -> bytes:
    raw = RULE_PATH.read_bytes()
    try:
        rule = raw.decode("utf-8")
    except UnicodeError as error:
        raise ModelCatalogError("Default 规则模板不是 UTF-8") from error
    if not rule.strip() or len(raw) > 32 * 1024:
        raise ModelCatalogError("Default 规则模板为空或超过读取上限")
    return raw


def derive(source_raw: bytes, rule_raw: bytes) -> bytes:
    source = parse_catalog(source_raw)
    try:
        rule = rule_raw.decode("utf-8")
    except (UnicodeError, AttributeError) as error:
        raise ModelCatalogError("Default 规则必须是 UTF-8 字节") from error
    if not rule.strip() or len(rule_raw) > 32 * 1024:
        raise ModelCatalogError("Default 规则为空或超过读取上限")
    derived = copy.deepcopy(source)
    for model in derived["models"]:
        if model.get("model_messages") is None:
            model["model_messages"] = {}
        messages = model["model_messages"]
        if messages.get("collaboration_modes") is None:
            messages["collaboration_modes"] = {}
        messages["collaboration_modes"]["default"] = rule
    return files.json_bytes(derived)


def verify(source_raw: bytes, derived_raw: bytes, rule_raw: bytes) -> None:
    actual = parse_catalog(derived_raw)
    expected = parse_catalog(derive(source_raw, rule_raw))
    if files.json_bytes(actual) != files.json_bytes(expected):
        raise ModelCatalogError("派生目录在许可 Default 规则路径之外变化")


def binary_identity(binary: Path) -> dict:
    binary = binary.resolve(strict=True)
    info = binary.stat()
    if not binary.is_file() or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
        raise ModelCatalogError("Codex 可执行文件归属或权限不安全")
    digest = hashlib.sha256()
    with binary.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    try:
        result = subprocess.run([str(binary), "--version"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ModelCatalogError("无法读取 Codex 二进制版本") from error
    match = re.fullmatch(rb"codex-cli (0\.172\.0)\s*", result.stdout) if result.returncode == 0 else None
    if match is None:
        raise ModelCatalogError("Codex 模型目录协议版本未经核验；保留当前目录")
    return {"path": str(binary), "sha256": digest.hexdigest(), "version": match[1].decode()}


def provider_scope(config: dict) -> tuple[dict, str]:
    name = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(name, str) or not isinstance(providers, dict) or not isinstance(providers.get(name), dict):
        raise ModelCatalogError("模型目录采集需要当前明确的 Responses provider")
    provider = providers[name]
    allowed = {"name", "base_url", "wire_api", "requires_openai_auth", "auth",
               "supports_standalone_web_search", "supports_responses_websockets",
               "request_max_retries", "stream_max_retries", "stream_idle_timeout_ms"}
    if set(provider) - allowed or provider.get("wire_api") != "responses":
        raise ModelCatalogError("当前 provider 包含未经核验的目录采集字段")
    url = urlsplit(provider.get("base_url", ""))
    if (url.scheme not in {"https", "http"} or not url.hostname or url.username
            or url.password or url.query or url.fragment or provider.get("requires_openai_auth") is not False):
        raise ModelCatalogError("当前 provider 来源或认证模式不适用隔离采集")
    auth = provider.get("auth")
    if (not isinstance(auth, dict) or set(auth) != {"command", "args"}
            or not isinstance(auth["command"], str) or not Path(auth["command"]).is_absolute()
            or not isinstance(auth["args"], list) or not all(isinstance(arg, str) for arg in auth["args"])):
        raise ModelCatalogError("隔离目录采集只使用已有外部认证命令，不复制认证文件")
    # No static catalog, model settings, hooks, projects or plugin configuration.
    native = {"model_provider": name, "model_providers": {name: copy.deepcopy(provider)},
              "features": {"hooks": False}, "analytics": {"enabled": False},
              "feedback": {"enabled": False}}
    return native, hashlib.sha256(files.json_bytes(native)).hexdigest()


def acquire(binary: Path, config: dict) -> tuple[bytes, dict]:
    """Capture the effective native catalog, not the endpoint cache projection."""
    identity = binary_identity(binary)
    native_config, scope = provider_scope(config)
    with tempfile.TemporaryDirectory(prefix="pennix-model-catalog-") as directory:
        root = Path(directory)
        native_home = root / "codex"
        native_home.mkdir(mode=0o700)
        (native_home / "config.toml").write_text(dumps(native_config), encoding="utf-8")
        (native_home / "config.toml").chmod(0o600)
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "HOME": str(root), "CODEX_HOME": str(native_home),
                       "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
                       "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        start = datetime.now(timezone.utc)
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = None
            try:
                process = subprocess.Popen([identity["path"], "debug", "models"], cwd=root, env=environment,
                                           stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                           start_new_session=True)
                code = process.wait(timeout=90)
            except (OSError, subprocess.TimeoutExpired) as error:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                raise ModelCatalogError("原生模型目录采集失败；旧代际保持") from error
            except BaseException:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                raise
            output.seek(0)
            source_raw = output.read(MAX_CATALOG_BYTES + 1)
        end = datetime.now(timezone.utc)
        if code != 0:
            raise ModelCatalogError("原生模型目录采集非零退出；不采用回退目录")
        source = parse_catalog(source_raw)
        cache_path = native_home / "models_cache.json"
        # A fresh private home starts empty. A valid newly fetched scoped cache is
        # required because debug models may otherwise succeed with bundled fallback.
        cache_raw = files.read(cache_path)
        if cache_raw is None:
            raise ModelCatalogError("无法证明本次目录取得了新鲜端点来源")
        try:
            cache = json.loads(cache_raw, object_pairs_hook=files.unique_pairs)
            fetched = datetime.fromisoformat(cache["fetched_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise ModelCatalogError("本次原生目录来源证明无效") from error
        if (not isinstance(cache, dict) or cache.get("client_version") != identity["version"]
                or not isinstance(cache.get("identity"), str) or not cache["identity"]
                or fetched.tzinfo is None or not start <= fetched <= end
                or not isinstance(cache.get("models"), list) or not cache["models"]):
            raise ModelCatalogError("原生目录来源的新鲜度、作用域或二进制身份不符")
        if binary_identity(binary) != identity:
            raise ModelCatalogError("Codex 二进制在采集期间改变")
        proof = {"schema": 1, "binary": identity, "provider_scope_sha256": scope,
                 "source_sha256": hashlib.sha256(source_raw).hexdigest(),
                 "fetched_at": cache["fetched_at"], "verified_at": end.isoformat(),
                 "endpoint_identity_sha256": hashlib.sha256(cache["identity"].encode()).hexdigest(),
                 "endpoint_models": len(cache["models"]), "effective_models": len(source["models"])}
        return source_raw, proof


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _proof(source: bytes, proof: dict) -> None:
    if (not isinstance(proof, dict) or set(proof) != {
            "schema", "binary", "provider_scope_sha256", "source_sha256", "fetched_at",
            "verified_at", "endpoint_identity_sha256", "endpoint_models", "effective_models"}
            or type(proof["schema"]) is not int or proof["schema"] != 1
            or proof["source_sha256"] != _sha(source)
            or type(proof["endpoint_models"]) is not int or proof["endpoint_models"] < 1
            or type(proof["effective_models"]) is not int
            or proof["effective_models"] != len(parse_catalog(source)["models"])
            or not isinstance(proof["binary"], dict)
            or set(proof["binary"]) != {"path", "sha256", "version"}
            or proof["binary"]["version"] != "0.172.0"
            or not isinstance(proof["binary"]["path"], str)
            or not Path(proof["binary"]["path"]).is_absolute()):
        raise ModelCatalogError("模型目录缺少可信的原生来源证明")
    for digest in (proof["provider_scope_sha256"], proof["endpoint_identity_sha256"],
                   proof["binary"]["sha256"]):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ModelCatalogError("模型目录来源摘要无效")
    try:
        fetched, verified = (datetime.fromisoformat(proof[key].replace("Z", "+00:00"))
                             for key in ("fetched_at", "verified_at"))
        if fetched.tzinfo is None or verified.tzinfo is None or fetched > verified:
            raise ValueError("invalid chronology")
    except (AttributeError, TypeError, ValueError) as error:
        raise ModelCatalogError("模型目录来源时间无效") from error


def candidate(source: bytes, proof: dict, rule: bytes | None = None) -> dict:
    """Build an in-memory candidate; planning does not publish active assets."""
    _proof(source, proof)
    rule = rule_bytes() if rule is None else rule
    contents = {"source.json": source, "derived.json": derive(source, rule),
                "rule.md": rule, "proof.json": files.json_bytes(proof)}
    hashes = {name: _sha(raw) for name, raw in contents.items()}
    return {"descriptor": {"schema": 1, "generation": _sha(files.json_bytes(hashes)), "files": hashes},
            "contents": contents}


def _directory(descriptor: dict) -> Path:
    if (not isinstance(descriptor, dict) or set(descriptor) != {"schema", "generation", "files"}
            or type(descriptor["schema"]) is not int or descriptor["schema"] != 1
            or not isinstance(descriptor["files"], dict) or set(descriptor["files"]) != ASSETS
            or not isinstance(descriptor["generation"], str)
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in descriptor["files"].values())
            or descriptor["generation"] != _sha(files.json_bytes(descriptor["files"]))):
        raise ModelCatalogError("模型目录代际归属无效")
    path = files.state_root() / "model-catalog" / "generations" / descriptor["generation"]
    files.safe_path(path)
    if path.exists() and not path.is_dir():
        raise ModelCatalogError("模型目录代际不是目录")
    return path


def reference(descriptor: dict) -> str:
    return str(_directory(descriptor) / "derived.json")


def read_generation(descriptor: dict) -> dict:
    path = _directory(descriptor)
    expected = ASSETS | {"manifest.json"}
    if not path.is_dir() or {entry.name for entry in path.iterdir()} != expected:
        raise ModelCatalogError("受管模型目录代际缺失或含未知文件")
    manifest = files.read(path / "manifest.json", private=True)
    if manifest != files.json_bytes(descriptor):
        raise ModelCatalogError("受管模型目录清单与归属不符")
    contents = {}
    for name, digest in descriptor["files"].items():
        raw = files.read(path / name, private=True, limit=MAX_CATALOG_BYTES)
        if raw is None or _sha(raw) != digest:
            raise ModelCatalogError("受管模型目录资产缺失或漂移")
        contents[name] = raw
    try:
        proof = json.loads(contents["proof.json"], object_pairs_hook=files.unique_pairs)
    except (UnicodeError, ValueError) as error:
        raise ModelCatalogError("模型目录来源证明无效") from error
    _proof(contents["source.json"], proof)
    verify(contents["source.json"], contents["derived.json"], contents["rule.md"])
    return {"descriptor": descriptor, "contents": contents}


def publish(value: dict, *, repairs: dict | None = None) -> dict:
    descriptor, contents = value["descriptor"], value["contents"]
    path = _directory(descriptor)
    if repairs is not None and (not isinstance(repairs, dict)
            or set(repairs) != {"derived.json", "manifest.json"}
            or any(digest is not None and (not isinstance(digest, str)
                   or not re.fullmatch(r"[0-9a-f]{64}", digest)) for digest in repairs.values())):
        raise ModelCatalogError("模型目录修复前值无效")
    if (set(contents) != ASSETS
            or any(not isinstance(raw, bytes) or _sha(raw) != descriptor["files"][name]
                   for name, raw in contents.items())):
        raise ModelCatalogError("已审阅模型目录候选改变")
    if path.exists() and any(entry.name not in ASSETS | {"manifest.json"} for entry in path.iterdir()):
        raise ModelCatalogError("模型目录代际含未知资产，未覆盖")
    # Immutable generation first; no config ever references a partial directory.
    for name, raw in {**contents, "manifest.json": files.json_bytes(descriptor)}.items():
        before = files.read(path / name, private=True, limit=MAX_CATALOG_BYTES)
        reviewed = repairs is not None and name in repairs
        if reviewed and before != raw and (_sha(before) if before is not None else None) != repairs[name]:
            raise ModelCatalogError("模型目录修复目标在审阅后改变")
        if before is not None and before != raw and not reviewed:
            raise ModelCatalogError("不可变模型目录代际已被外部修改")
        files.replace(path / name, raw, before, mode=0o400)
    read_generation(descriptor)
    return descriptor


def repair_candidate(descriptor: dict) -> tuple[dict, dict]:
    path = _directory(descriptor)
    outputs = {"derived.json", "manifest.json"}
    if not path.is_dir() or any(entry.name not in ASSETS | {"manifest.json"} for entry in path.iterdir()):
        raise ModelCatalogError("模型目录修复目标缺失或含未知资产")
    fixed = {}
    for name in ("source.json", "rule.md", "proof.json"):
        raw = files.read(path / name, private=True)
        if raw is None or _sha(raw) != descriptor["files"][name]:
            raise ModelCatalogError("固定源、规则或来源证明丢失／漂移；不能以修复重新取得来源")
        fixed[name] = raw
    try:
        proof = json.loads(fixed["proof.json"], object_pairs_hook=files.unique_pairs)
    except (UnicodeError, ValueError) as error:
        raise ModelCatalogError("模型目录修复的来源证明无效") from error
    value = candidate(fixed["source.json"], proof, fixed["rule.md"])
    if value["descriptor"] != descriptor:
        raise ModelCatalogError("固定源不能重建已登记的模型目录代际")
    observed = {}
    for name in outputs:
        raw = files.read(path / name, private=True)
        observed[name] = _sha(raw) if raw is not None else None
    return value, observed


def compatible(value: dict, binary: Path, config: dict) -> None:
    proof = json.loads(value["contents"]["proof.json"])
    if (binary_identity(binary) != proof["binary"]
            or provider_scope(config)[1] != proof["provider_scope_sha256"]):
        raise ModelCatalogError("固定模型目录与当前 Codex 或 provider 不兼容；请显式更新")


def with_current_rule(descriptor: dict) -> dict:
    value = read_generation(descriptor)
    return candidate(value["contents"]["source.json"],
                     json.loads(value["contents"]["proof.json"]))


def remove_generation(descriptor: dict) -> None:
    """Withdraw only exact owned bytes; missing files allow same-journal recovery."""
    path = _directory(descriptor)
    if not path.exists():
        return
    expected = {**descriptor["files"], "manifest.json": _sha(files.json_bytes(descriptor))}
    if any(entry.name not in expected for entry in path.iterdir()):
        raise ModelCatalogError("模型目录含未知资产，未清理")
    remaining = {}
    for name, digest in expected.items():
        raw = files.read(path / name, private=True, limit=MAX_CATALOG_BYTES)
        if raw is not None:
            if _sha(raw) != digest:
                raise ModelCatalogError("模型目录资产漂移，未清理")
            remaining[name] = raw
    for name, raw in remaining.items():
        files.replace(path / name, None, raw)
    path.rmdir()
    files.sync_directory(path.parent)


def _review_path(digest: str) -> Path:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ModelCatalogError("模型目录审阅摘要无效")
    return files.xdg_home("XDG_CACHE_HOME", Path.home() / ".cache") / (
        "pennix-lifecycle/model-catalog-candidates") / digest


def stage_review(value: dict, context: dict) -> dict:
    descriptor = value["descriptor"]
    review = {"schema": 1, "context": context, "catalog": descriptor}
    raw = files.json_bytes(review)
    digest = _sha(raw)
    root = _review_path(digest)
    for name, content in {**value["contents"], "review.json": raw}.items():
        previous = files.read(root / name, private=True)
        if previous is not None and previous != content:
            raise ModelCatalogError("已冻结目录候选被修改，未覆盖")
        files.replace(root / name, content, previous, mode=0o400)
    return {**review, "review_sha256": digest}


def load_review(digest: str, context: dict) -> dict:
    root = _review_path(digest)
    files.safe_path(root)
    if not root.is_dir() or {entry.name for entry in root.iterdir()} != ASSETS | {"review.json"}:
        raise ModelCatalogError("模型目录冻结候选缺失或有未知文件")
    raw = files.read(root / "review.json", private=True)
    if raw is None or _sha(raw) != digest:
        raise ModelCatalogError("模型目录冻结审阅材料摘要不符")
    try:
        review = json.loads(raw, object_pairs_hook=files.unique_pairs)
    except (UnicodeError, ValueError) as error:
        raise ModelCatalogError("模型目录冻结审阅材料无效") from error
    if (not isinstance(review, dict) or set(review) != {"schema", "context", "catalog"}
            or type(review["schema"]) is not int or review["schema"] != 1
            or review["context"] != context):
        raise ModelCatalogError("模型目录候选的目标、配置、归属、工具或锁已改变；请重新审阅")
    descriptor = review["catalog"]
    _directory(descriptor)
    contents = {name: files.read(root / name, private=True) for name in ASSETS}
    if any(raw is None or _sha(raw) != descriptor["files"][name] for name, raw in contents.items()):
        raise ModelCatalogError("模型目录冻结资产摘要不符")
    try:
        proof = json.loads(contents["proof.json"], object_pairs_hook=files.unique_pairs)
    except (UnicodeError, ValueError) as error:
        raise ModelCatalogError("模型目录冻结来源证明无效") from error
    value = candidate(contents["source.json"], proof, contents["rule.md"])
    if value["descriptor"] != descriptor or contents["rule.md"] != rule_bytes():
        raise ModelCatalogError("模型目录冻结派生或当前规则已改变；请重新审阅")
    return value
