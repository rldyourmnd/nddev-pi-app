"""Dependency-free public provider protocol v3 primitives.

This module is intentionally vendored in the public provider.  It consumes the
immutable ``ai-stp-provider-conformance-kit/1`` contract but has no runtime
dependency on either private control-plane repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, NoReturn

PROTOCOL_VERSION: Final[int] = 3
BUNDLE_FORMAT: Final[str] = "ai-stp-bundle/1"
BUNDLE_PROTOCOL_VERSION: Final[int] = 1
PLAN_FORMAT: Final[str] = "ai-stp-provider-plan/3"
STATE_FORMAT: Final[str] = "ai-stp-provider-state/3"
CORE_COMMANDS: Final[tuple[str, ...]] = (
    "provider-info",
    "validate-bundle",
    "plan-operation",
    "apply-operation",
    "recover-operation",
    "status",
)
OPTIONAL_COMMANDS: Final[tuple[str, ...]] = ("launch",)
COMMANDS: Final[tuple[str, ...]] = (*CORE_COMMANDS, *OPTIONAL_COMMANDS)
CORE_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"install", "replace", "backup", "restore", "remove"}
)
OPTIONAL_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"software_install", "software_update", "software_remove", "launch"}
)
ALL_OPERATIONS: Final[frozenset[str]] = CORE_OPERATIONS | OPTIONAL_OPERATIONS
COMPONENT_KINDS: Final[frozenset[str]] = frozenset(
    {"instruction", "skill", "mcp", "hook", "command", "agent", "plugin", "setting"}
)
PROJECTION_KINDS: Final[frozenset[str]] = frozenset(
    {"marketplace", "plugin", "native_files", "package"}
)
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
ZIP_TIMESTAMP: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)
MAX_FILES: Final[int] = 2000
MAX_FILE_BYTES: Final[int] = 4 * 1024 * 1024
MAX_BUNDLE_BYTES: Final[int] = 64 * 1024 * 1024


class ProtocolError(Exception):
    """Stable fail-closed provider error safe to expose as JSON."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


def refuse(reason: str, message: str) -> NoReturn:
    raise ProtocolError(reason, message)


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        refuse(
            "canonical_json_invalid",
            "floating point is not allowed in provider artifacts",
        )
    if isinstance(value, str):
        if "\ufeff" in value:
            refuse("canonical_json_invalid", "byte-order marks are not allowed")
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                refuse("canonical_json_invalid", "JSON object keys must be strings")
            held = unicodedata.normalize("NFC", key)
            if held in normalized:
                refuse("canonical_json_invalid", "JSON keys collide after normalization")
            normalized[held] = _normalize(item)
        return normalized
    refuse("canonical_json_invalid", f"unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Canonical bytes for the integer/string-only provider wire model."""
    return json.dumps(
        _normalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def canonical_digest(domain: str, value: Any) -> str:
    digest = hashlib.sha256(domain.encode("ascii") + b"\x00" + canonical_json(value))
    return f"sha256:{digest.hexdigest()}"


def raw_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def require_digest(value: str, field: str) -> None:
    if SHA256.fullmatch(value) is None:
        refuse("digest_mismatch", f"{field} is not canonical SHA-256")


def load_json_bytes(content: bytes, label: str) -> Any:
    if content.startswith(b"\xef\xbb\xbf"):
        refuse("canonical_json_invalid", f"{label} starts with a byte-order mark")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        answer: dict[str, Any] = {}
        for key, value in values:
            if key in answer:
                refuse("canonical_json_invalid", f"{label} has a duplicate key")
            answer[key] = value
        return answer

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_float=lambda _: refuse(
                "canonical_json_invalid", f"{label} contains a floating-point value"
            ),
            parse_constant=lambda _: refuse(
                "canonical_json_invalid", f"{label} contains a non-finite value"
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        refuse("canonical_json_invalid", f"{label} is not valid JSON: {exc}")
    if canonical_json(value) != content:
        refuse("canonical_json_invalid", f"{label} is not canonical JSON")
    return value


@dataclass(frozen=True)
class BundleIdentity:
    path: Path
    bundle_digest: str
    artifact_digest: str
    bundle_size: int
    manifest: dict[str, Any]
    setup_passport: dict[str, Any]
    components: tuple[dict[str, str], ...]
    conversions: tuple[dict[str, str], ...]
    owned_files: tuple[dict[str, Any], ...]

    def echoes(self) -> dict[str, Any]:
        return {
            "bundle_format": BUNDLE_FORMAT,
            "bundle_digest": self.bundle_digest,
            "artifact_digest": self.artifact_digest,
            "bundle_size": self.bundle_size,
        }


def _regular_absolute(path: Path) -> os.stat_result:
    if not path.is_absolute():
        refuse("path_not_relative", "bundle path must be absolute")
    try:
        held = path.lstat()
    except OSError as exc:
        refuse("digest_mismatch", f"cannot inspect bundle: {exc}")
    if stat.S_ISLNK(held.st_mode) or not stat.S_ISREG(held.st_mode) or held.st_nlink != 1:
        refuse("link_not_allowed", "bundle must be one regular non-linked file")
    return held


def _member_path(name: str) -> str:
    normalized = unicodedata.normalize("NFC", name.replace("\\", "/"))
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith(("/", "~")) or path.is_absolute():
        refuse("path_not_relative", "bundle member path must be relative")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        refuse("path_escapes_target", "bundle member path contains traversal")
    return normalized


def _member_mode(info: zipfile.ZipInfo) -> tuple[int, int]:
    raw = info.external_attr >> 16
    return stat.S_IFMT(raw), stat.S_IMODE(raw)


def validate_bundle(
    path: Path,
    *,
    expected_harness: str,
    expected_bundle_digest: str,
    expected_artifact_digest: str,
    expected_size: int,
    supported_components: frozenset[str],
    supported_surfaces: frozenset[str],
) -> BundleIdentity:
    """Validate exact ZIP bytes and native projection without extracting."""
    require_digest(expected_bundle_digest, "bundle_digest")
    require_digest(expected_artifact_digest, "artifact_digest")
    held = _regular_absolute(path)
    if held.st_size <= 0 or held.st_size != expected_size or held.st_size > MAX_BUNDLE_BYTES:
        refuse("limit_exceeded", "bundle size differs from its exact binding")
    content = path.read_bytes()
    if raw_digest(content) != expected_artifact_digest:
        refuse("digest_mismatch", "raw bundle bytes do not match artifact_digest")
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        refuse("digest_mismatch", f"bundle is not a valid ZIP: {exc}")
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES + 6:
            refuse("limit_exceeded", "bundle has too many ZIP members")
        names = [
            _member_path(info.filename.rstrip("/")) + ("/" if info.is_dir() else "")
            for info in infos
        ]
        if len(names) != len(set(names)):
            refuse("path_duplicate", "bundle has duplicate normalized members")
        if any(info.date_time != ZIP_TIMESTAMP or info.create_system != 3 for info in infos):
            refuse("digest_mismatch", "bundle member timestamp is not deterministic")
        if any(info.compress_type != zipfile.ZIP_STORED for info in infos):
            refuse("digest_mismatch", "bundle compression is not supported")
        if any(info.extra or info.comment or info.flag_bits & 0x1 for info in infos):
            refuse("digest_mismatch", "bundle metadata is not deterministic")
        if any(info.file_size > MAX_FILE_BYTES for info in infos if not info.is_dir()):
            refuse("limit_exceeded", "bundle member exceeds the file limit")
        by_name = {info.filename: info for info in infos}
        required = {
            "bundle.json",
            "setup-passport.json",
            "composition-report.json",
            "conversion-report.json",
            "files/",
            "attestations/",
        }
        if not required <= set(by_name):
            refuse("digest_mismatch", "bundle is missing a required member")
        for info in infos:
            kind, mode = _member_mode(info)
            if info.is_dir():
                if kind != stat.S_IFDIR or mode != 0o755:
                    refuse(
                        "special_file_not_allowed",
                        "bundle directory metadata is invalid",
                    )
            elif kind == stat.S_IFLNK:
                refuse("link_not_allowed", "bundle symbolic links are forbidden")
            elif kind != stat.S_IFREG or mode not in {0o644, 0o755}:
                refuse("special_file_not_allowed", "bundle member is not a permitted file")
        documents = {
            name: load_json_bytes(archive.read(name), name)
            for name in (
                "bundle.json",
                "setup-passport.json",
                "composition-report.json",
                "conversion-report.json",
            )
        }
        manifest = documents["bundle.json"]
        passport = documents["setup-passport.json"]
        composition = documents["composition-report.json"]
        conversion = documents["conversion-report.json"]
        if (
            not isinstance(manifest, dict)
            or not isinstance(passport, dict)
            or not isinstance(composition, dict)
        ):
            refuse(
                "digest_mismatch",
                "bundle manifest, passport and reports must be objects",
            )
        if manifest.get("bundle_format") != BUNDLE_FORMAT:
            refuse("unsupported_bundle_format", "provider supports ai-stp-bundle/1 only")
        if manifest.get("protocol_version") != BUNDLE_PROTOCOL_VERSION:
            refuse("unsupported_protocol_version", "bundle protocol version is unsupported")
        if manifest.get("harness_id") != expected_harness:
            refuse("unsupported_native_surface", "bundle targets another harness")
        manifest_identity = dict(manifest)
        observed_digest = manifest_identity.pop("bundle_digest", None)
        computed_digest = canonical_digest("ai-stp:bundle:v1", manifest_identity)
        if observed_digest != expected_bundle_digest or computed_digest != expected_bundle_digest:
            refuse("digest_mismatch", "logical bundle digest does not match its manifest")
        if not isinstance(conversion, dict) or conversion.get("complete") is not True:
            refuse("unsupported_native_surface", "bundle conversion is incomplete")
        entries = conversion.get("entries")
        if not isinstance(entries, list):
            refuse("unsupported_native_surface", "bundle conversion entries are missing")
        converted_ids: list[str] = []
        conversions: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                refuse("unsupported_native_surface", "bundle conversion entry is invalid")
            kind = entry.get("component_type")
            surface = entry.get("native_surface")
            stable_id = entry.get("stable_id")
            if not isinstance(stable_id, str) or not stable_id:
                refuse(
                    "unsupported_native_surface",
                    "conversion component identity is missing",
                )
            converted_ids.append(stable_id)
            if kind not in supported_components:
                refuse(
                    "unsupported_component_kind",
                    f"component kind is unsupported: {kind}",
                )
            if surface not in supported_surfaces or entry.get("state") != "complete":
                refuse(
                    "unsupported_native_surface",
                    f"native surface is unsupported: {surface}",
                )
            projection_kind = entry.get("projection_kind", "native_files")
            if projection_kind not in PROJECTION_KINDS:
                refuse(
                    "unsupported_native_surface",
                    f"projection kind is unsupported: {projection_kind}",
                )
            conversions.append(
                {
                    "stable_id": stable_id,
                    "component_type": str(kind),
                    "native_surface": str(surface),
                    "projection_kind": str(projection_kind),
                }
            )
        if len(converted_ids) != len(set(converted_ids)):
            refuse("path_duplicate", "bundle conversion repeats a component identity")
        raw_files = manifest.get("files")
        if not isinstance(raw_files, list):
            refuse("digest_mismatch", "bundle manifest files are missing")
        expected_file_names: list[str] = []
        owned: list[dict[str, Any]] = []
        for item in raw_files:
            if not isinstance(item, dict):
                refuse("digest_mismatch", "bundle file record is invalid")
            declared_kind = item.get("kind")
            if declared_kind in {"symlink", "hardlink"}:
                refuse("link_not_allowed", "bundle link records are forbidden")
            if declared_kind == "special":
                refuse(
                    "special_file_not_allowed",
                    "bundle special-file records are forbidden",
                )
            byte_length = item.get("byte_length")
            if (
                not isinstance(byte_length, int)
                or isinstance(byte_length, bool)
                or byte_length < 0
                or byte_length > MAX_FILE_BYTES
            ):
                refuse("limit_exceeded", "bundle file record exceeds the byte limit")
            relative = _member_path(str(item.get("path", "")))
            if not any(
                relative == surface or relative.startswith(f"{surface}/")
                for surface in supported_surfaces
            ):
                refuse(
                    "unsupported_native_surface",
                    f"managed path is outside declared native surfaces: {relative}",
                )
            member = f"files/{relative}"
            expected_file_names.append(member)
            info = by_name.get(member)
            if info is None or info.is_dir():
                refuse("digest_mismatch", f"managed file is absent: {relative}")
            payload = archive.read(info)
            digest = str(item.get("digest", ""))
            if raw_digest(payload) != digest or len(payload) != item.get("byte_length"):
                refuse("digest_mismatch", f"managed file identity differs: {relative}")
            _, mode = _member_mode(info)
            if mode != item.get("mode"):
                refuse("digest_mismatch", f"managed file mode differs: {relative}")
            owned.append(
                {
                    "path": relative,
                    "digest": digest,
                    "byte_length": len(payload),
                    "mode": mode,
                    "owner": str(item.get("owner", "")),
                }
            )
        observed_file_names = [
            info.filename
            for info in infos
            if info.filename.startswith("files/") and not info.is_dir()
        ]
        if observed_file_names != expected_file_names:
            refuse(
                "path_duplicate",
                "bundle file order or membership differs from manifest",
            )
        exact_order = [
            "bundle.json",
            "setup-passport.json",
            "composition-report.json",
            "conversion-report.json",
            "files/",
            *expected_file_names,
            "attestations/",
        ]
        if [info.filename for info in infos] != exact_order:
            refuse(
                "path_duplicate",
                "bundle ZIP order or membership differs from the format",
            )
        if manifest.get("managed_paths") != [item["path"] for item in owned]:
            refuse("digest_mismatch", "managed_paths differs from the file manifest")
        document_manifest = manifest.get("documents")
        if not isinstance(document_manifest, dict):
            refuse("digest_mismatch", "bundle document manifest is missing")
        for key, name in (
            ("setup_passport", "setup-passport.json"),
            ("composition_report", "composition-report.json"),
            ("conversion_report", "conversion-report.json"),
        ):
            record = document_manifest.get(key)
            payload = archive.read(name)
            if (
                not isinstance(record, dict)
                or record.get("path") != name
                or record.get("digest") != raw_digest(payload)
                or record.get("byte_length") != len(payload)
            ):
                refuse("digest_mismatch", f"document identity differs: {name}")
        setup = manifest.get("setup")
        components = passport.get("components")
        if not isinstance(setup, dict) or not isinstance(components, list):
            refuse("digest_mismatch", "setup or exact component references are missing")
        setup_digest = str(setup.get("passport_digest", ""))
        if setup.get("stable_id") != passport.get("stable_id") or setup_digest != canonical_digest(
            "ai-stp:passport:v1", passport
        ):
            refuse("digest_mismatch", "SetupVersion passport digest differs")
        exact_components: list[dict[str, str]] = []
        for component in components:
            if not isinstance(component, dict):
                refuse("digest_mismatch", "component reference is invalid")
            exact = {
                "stable_id": str(component.get("stable_id", "")),
                "version": str(component.get("version", "")),
                "passport_digest": str(component.get("passport_digest", "")),
            }
            if not exact["stable_id"] or not exact["version"]:
                refuse("digest_mismatch", "component reference identity is incomplete")
            require_digest(exact["passport_digest"], "component passport_digest")
            exact_components.append(exact)
        component_ids = [item["stable_id"] for item in exact_components]
        if len(component_ids) != len(set(component_ids)):
            refuse("path_duplicate", "bundle repeats an exact component identity")
        if set(converted_ids) != set(component_ids):
            refuse("digest_mismatch", "conversion report differs from exact components")
        if any(item["owner"] not in set(component_ids) for item in owned):
            refuse("digest_mismatch", "managed file owner is not an exact component")
        owned_component_ids = {item["owner"] for item in owned}
        if owned_component_ids != set(component_ids):
            refuse("digest_mismatch", "every exact component must own native bundle content")
        chosen = composition.get("chosen")
        if not isinstance(chosen, list):
            refuse("digest_mismatch", "composition report choices are missing")
        chosen_refs = {
            (str(item.get("stable_id", "")), str(item.get("version", "")))
            for item in chosen
            if isinstance(item, dict)
        }
        component_refs = {(item["stable_id"], item["version"]) for item in exact_components}
        if chosen_refs != component_refs:
            refuse("digest_mismatch", "composition report differs from exact components")
        return BundleIdentity(
            path=path,
            bundle_digest=expected_bundle_digest,
            artifact_digest=expected_artifact_digest,
            bundle_size=expected_size,
            manifest=manifest,
            setup_passport=passport,
            components=tuple(exact_components),
            conversions=tuple(conversions),
            owned_files=tuple(owned),
        )


def build_digest(root: Path) -> str:
    return raw_digest((root / "build" / "manifest.json").read_bytes())


def projection_digest(profile: dict[str, Any]) -> str:
    return canonical_digest("ai-stp:provider-projection:v3", profile)


def provider_info(
    root: Path,
    *,
    provider_id: str,
    harness_id: str,
    provider_version: str,
    operations: frozenset[str],
    profile: dict[str, Any],
    permission_profiles: tuple[str, ...] = (),
) -> dict[str, Any]:
    unknown = operations - ALL_OPERATIONS
    missing = CORE_OPERATIONS - operations
    if unknown or missing:
        refuse("unsupported_operation", "provider operation declaration is not conforming")
    projection = dict(profile)
    projection["digest"] = projection_digest(profile)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "provider_id": provider_id,
        "harness_id": harness_id,
        "provider_version": provider_version,
        "provider_build_digest": build_digest(root),
        "supported_commands": [
            *CORE_COMMANDS,
            *(("launch",) if "launch" in operations else ()),
        ],
        "supported_operations": sorted(operations),
        "supported_os": ["linux", "macos"],
        "supported_arch": ["arm64", "x86_64"],
        "permission_profiles": list(permission_profiles),
        "projection_profile": projection,
    }


def target_digest(target: Path, managed_paths: tuple[Path, ...]) -> str:
    records: list[dict[str, Any]] = []
    for path in sorted(managed_paths, key=str):
        relative = str(path.relative_to(target)) if path.is_relative_to(target) else str(path)
        try:
            held = path.lstat()
        except FileNotFoundError:
            records.append({"path": relative, "state": "missing"})
            continue
        if stat.S_ISLNK(held.st_mode):
            refuse("target_snapshot_invalid", f"managed target path is a symlink: {relative}")
        if not stat.S_ISREG(held.st_mode):
            refuse(
                "target_snapshot_invalid",
                f"managed target path is not regular: {relative}",
            )
        if held.st_nlink != 1:
            refuse("target_snapshot_invalid", f"managed target path is hardlinked: {relative}")
        content = path.read_bytes()
        records.append(
            {
                "path": relative,
                "state": "file",
                "digest": raw_digest(content),
                "mode": stat.S_IMODE(held.st_mode),
            }
        )
    return canonical_digest("nddev:provider-target:v3", records)


def plan_artifact(
    *,
    provider_id: str,
    provider_version: str,
    provider_build_digest: str,
    provider_release_digest: str,
    operation_id: str,
    operation: str,
    canonical_target: Path,
    expected_target_digest: str,
    projection_profile_digest: str,
    bundle: BundleIdentity | None,
    backup_ref: str | None,
    restore_target_digest: str | None,
    permission_profile: str | None,
    effects: tuple[str, ...],
    expires_at: str,
) -> tuple[dict[str, Any], str]:
    if operation not in ALL_OPERATIONS:
        refuse("unsupported_operation", f"unknown operation: {operation}")
    if operation in {"install", "replace"} and bundle is None:
        refuse("digest_mismatch", "install and replace require an exact bundle")
    if operation == "restore" and not backup_ref:
        refuse("backup_ref_invalid", "restore requires an exact BackupRef")
    if operation == "restore":
        require_digest(str(restore_target_digest or ""), "restore_target_digest")
    elif restore_target_digest is not None:
        refuse("plan_invalid", "only restore may bind a restored target digest")
    require_digest(provider_build_digest, "provider_build_digest")
    require_digest(provider_release_digest, "provider_release_digest")
    require_digest(expected_target_digest, "expected_target_digest")
    require_digest(projection_profile_digest, "projection_profile_digest")
    if not operation_id or len(operation_id) > 200:
        refuse("operation_id_invalid", "operation id must be non-empty and bounded")
    artifact: dict[str, Any] = {
        "format": PLAN_FORMAT,
        "protocol_version": PROTOCOL_VERSION,
        "provider_id": provider_id,
        "provider_version": provider_version,
        "provider_build_digest": provider_build_digest,
        "provider_release_digest": provider_release_digest,
        "operation_id": operation_id,
        "operation": operation,
        "canonical_target": str(canonical_target),
        "expected_target_digest": expected_target_digest,
        "projection_profile_digest": projection_profile_digest,
        "bundle": None if bundle is None else bundle.echoes(),
        "backup_ref": backup_ref,
        "restore_target_digest": restore_target_digest,
        "permission_profile": permission_profile,
        "platform": canonical_platform(),
        "expires_at": expires_at,
        "effects": list(effects),
    }
    return artifact, canonical_digest("ai-stp:provider-plan:v3", artifact)


def canonical_platform() -> dict[str, str]:
    """Return the protocol's cross-provider OS/architecture identity."""
    system = platform.system().casefold()
    os_name = "macos" if system == "darwin" else system
    machine = platform.machine().casefold()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return {"os": os_name, "arch": architecture}
