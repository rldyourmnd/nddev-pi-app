"""Transactional runtime for the public provider protocol v3.

The runtime deliberately has no dependency on ai_stp or the private validation
control plane.  It consumes the vendored wire primitives and owns only the
native files named by an exact HarnessBundle plus its own state and backup
directory.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import secrets
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import provider_protocol_v3 as v3

OWNER_DIR_MODE = 0o700
OWNER_FILE_MODE = 0o600
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_BACKUPS = 10


@dataclass(frozen=True)
class Config:
    root: Path
    provider_id: str
    harness_id: str
    provider_version: str
    state_name: str
    backup_directory: str
    component_kinds: frozenset[str]
    native_namespaces: frozenset[str]
    permission_profiles: tuple[str, ...] = ()

    @property
    def operations(self) -> frozenset[str]:
        return v3.CORE_OPERATIONS


@dataclass(frozen=True)
class Snapshot:
    present: bool
    content: bytes = b""
    mode: int = OWNER_FILE_MODE


class Runtime:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _profile(self) -> dict[str, Any]:
        return {
            "profile_id": f"{self.config.provider_id}/v3",
            "component_kinds": sorted(self.config.component_kinds),
            "projection_kinds": ["native_files"],
            "native_namespaces": sorted(self.config.native_namespaces),
            "bundle_formats": [v3.BUNDLE_FORMAT],
            "max_files": v3.MAX_FILES,
            "max_bytes": v3.MAX_BUNDLE_BYTES,
        }

    def info(self) -> dict[str, Any]:
        return v3.provider_info(
            self.config.root,
            provider_id=self.config.provider_id,
            harness_id=self.config.harness_id,
            provider_version=self.config.provider_version,
            operations=self.config.operations,
            profile=self._profile(),
            permission_profiles=self.config.permission_profiles,
        )

    @staticmethod
    def _optional_lstat(path: Path) -> os.stat_result | None:
        try:
            return path.lstat()
        except FileNotFoundError:
            return None

    @staticmethod
    def _target_key(target: Path) -> str:
        return str(target.resolve(strict=False))

    @staticmethod
    def _require_target(target: Path) -> os.stat_result:
        if not target.is_absolute():
            raise v3.ProtocolError("target_snapshot_invalid", "target must be absolute")
        held = Runtime._optional_lstat(target)
        if held is None:
            raise v3.ProtocolError("target_missing", "provider target must already exist")
        if stat.S_ISLNK(held.st_mode) or not stat.S_ISDIR(held.st_mode):
            raise v3.ProtocolError("link_not_allowed", "provider target must be a real directory")
        if hasattr(os, "geteuid") and held.st_uid != os.geteuid():
            raise v3.ProtocolError(
                "target_snapshot_invalid", "provider target is not owner-controlled"
            )
        return held

    @staticmethod
    def _safe_relative(value: object) -> str:
        if not isinstance(value, str) or not value or value.startswith(("/", "~")):
            raise v3.ProtocolError("path_not_relative", "provider state path is not relative")
        parts = Path(value).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise v3.ProtocolError("path_escapes_target", "provider state path leaves target")
        return Path(*parts).as_posix()

    @staticmethod
    def _read(path: Path, *, owner_only: bool = False) -> Snapshot:
        held = Runtime._optional_lstat(path)
        if held is None:
            return Snapshot(False)
        if stat.S_ISLNK(held.st_mode) or not stat.S_ISREG(held.st_mode) or held.st_nlink != 1:
            raise v3.ProtocolError(
                "link_not_allowed", f"provider path is not one regular file: {path}"
            )
        if hasattr(os, "geteuid") and held.st_uid != os.geteuid():
            raise v3.ProtocolError(
                "target_snapshot_invalid", f"provider path has another owner: {path}"
            )
        mode = stat.S_IMODE(held.st_mode)
        allowed = {OWNER_FILE_MODE} if owner_only else {0o600, 0o644, 0o755}
        if mode not in allowed or held.st_size > MAX_STATE_BYTES:
            raise v3.ProtocolError(
                "target_snapshot_invalid", f"provider path metadata is unsafe: {path}"
            )
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (held.st_dev, held.st_ino):
                raise v3.ProtocolError("target_snapshot_invalid", f"provider path changed: {path}")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                content = handle.read(MAX_STATE_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(content) > MAX_STATE_BYTES:
            raise v3.ProtocolError("limit_exceeded", f"provider path is too large: {path}")
        return Snapshot(True, content, mode)

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )

    def _state_path(self, target: Path) -> Path:
        return target / self.config.state_name

    def _state(self, target: Path) -> dict[str, Any] | None:
        snapshot = self._read(self._state_path(target), owner_only=True)
        if not snapshot.present:
            return None
        try:
            value = json.loads(snapshot.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise v3.ProtocolError(
                "target_snapshot_invalid", "provider state is not valid JSON"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("state_schema") != v3.STATE_FORMAT
            or value.get("protocol_version") != v3.PROTOCOL_VERSION
            or value.get("provider_id") != self.config.provider_id
            or value.get("canonical_target") != self._target_key(target)
        ):
            raise v3.ProtocolError("target_snapshot_invalid", "provider state identity is invalid")
        ownership = value.get("native_ownership")
        if not isinstance(ownership, list):
            raise v3.ProtocolError("target_snapshot_invalid", "provider state ownership is absent")
        seen: set[str] = set()
        for item in ownership:
            if not isinstance(item, dict):
                raise v3.ProtocolError(
                    "target_snapshot_invalid", "provider ownership entry is invalid"
                )
            relative = self._safe_relative(item.get("path"))
            if relative in seen:
                raise v3.ProtocolError("path_duplicate", "provider state repeats an owned path")
            seen.add(relative)
            v3.require_digest(str(item.get("digest", "")), "native ownership digest")
            if item.get("mode") not in {0o600, 0o644, 0o755}:
                raise v3.ProtocolError(
                    "target_snapshot_invalid", "provider ownership mode is invalid"
                )
        return value

    def _owned_paths(self, target: Path, state: dict[str, Any] | None) -> tuple[Path, ...]:
        if state is None:
            return ()
        return tuple(
            target / self._safe_relative(item["path"]) for item in state["native_ownership"]
        )

    def _target_digest(self, target: Path) -> str:
        state = self._state(target)
        native = v3.target_digest(target, self._owned_paths(target, state))
        identity: dict[str, Any] | None = None
        if state is not None:
            identity = dict(state)
            identity.pop("target_identity_digest", None)
        return v3.canonical_digest(
            f"{self.config.provider_id}:provider-target:v3",
            {"native": native, "state": identity},
        )

    def status(self, target: Path) -> dict[str, Any]:
        held = self._optional_lstat(target)
        if held is None:
            return {
                "state": "missing",
                "target_digest": v3.canonical_digest(
                    f"{self.config.provider_id}:provider-target:v3",
                    {
                        "native": v3.canonical_digest("nddev:provider-target:v3", []),
                        "state": None,
                    },
                ),
            }
        self._require_target(target)
        state = self._state(target)
        target_digest = self._target_digest(target)
        if state is None:
            return {"state": "unmanaged", "target_digest": target_digest}
        drift: list[str] = []
        for item in state["native_ownership"]:
            relative = self._safe_relative(item["path"])
            snapshot = self._read(target / relative)
            if (
                not snapshot.present
                or v3.raw_digest(snapshot.content) != item.get("digest")
                or snapshot.mode != item.get("mode")
            ):
                drift.append(relative)
        if state.get("target_identity_digest") != target_digest:
            drift.append(self.config.state_name)
        answer = dict(state)
        answer.update(
            {
                "state": "managed",
                "target_digest": target_digest,
                "drift_state": "verified" if not drift else "drifted",
                "drift": drift,
            }
        )
        return answer

    def _bundle(self, args: Any) -> v3.BundleIdentity:
        if getattr(args, "bundle_format", None) != v3.BUNDLE_FORMAT:
            raise v3.ProtocolError(
                "unsupported_bundle_format",
                f"provider supports {v3.BUNDLE_FORMAT} only",
            )
        raw_path = getattr(args, "bundle", None)
        if not isinstance(raw_path, str) or not raw_path:
            raise v3.ProtocolError("digest_mismatch", "bundle path is required")
        size = getattr(args, "bundle_size", None)
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise v3.ProtocolError("limit_exceeded", "exact positive bundle size is required")
        return v3.validate_bundle(
            Path(raw_path),
            expected_harness=self.config.harness_id,
            expected_bundle_digest=str(getattr(args, "bundle_digest", "")),
            expected_artifact_digest=str(getattr(args, "artifact_digest", "")),
            expected_size=size,
            supported_components=self.config.component_kinds,
            supported_surfaces=self.config.native_namespaces,
        )

    def validate(self, args: Any) -> dict[str, Any]:
        bundle = self._bundle(args)
        return {**bundle.echoes(), "valid": True}

    def plan(self, target: Path, args: Any) -> dict[str, Any]:
        self._require_target(target)
        operation = str(getattr(args, "operation", ""))
        if operation not in self.config.operations:
            raise v3.ProtocolError(
                "unsupported_operation", f"operation is unsupported: {operation}"
            )
        current = self._state(target)
        if operation == "install" and current is not None:
            raise v3.ProtocolError("target_already_managed", "install requires an unmanaged target")
        if operation in {"replace", "backup", "remove"} and current is None:
            raise v3.ProtocolError(
                "target_unmanaged", f"{operation} requires provider-managed state"
            )
        bundle = self._bundle(args) if operation in {"install", "replace"} else None
        permission_profile = getattr(args, "permission_profile", None)
        if (
            permission_profile is not None
            and permission_profile not in self.config.permission_profiles
        ):
            raise v3.ProtocolError(
                "unsupported_permission_profile", "permission profile is unsupported"
            )
        expires_at = str(getattr(args, "expires_at", ""))
        try:
            expiry = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise v3.ProtocolError("plan_expired", "plan expiry is invalid") from exc
        if expiry.tzinfo is None or expiry <= dt.datetime.now(dt.timezone.utc):
            raise v3.ProtocolError("plan_expired", "plan expiry must be in the future")
        effects: tuple[str, ...]
        same_bundle = (
            operation == "replace"
            and bundle is not None
            and current is not None
            and current.get("bundle_digest") == bundle.bundle_digest
            and current.get("artifact_digest") == bundle.artifact_digest
            and current.get("permission_profile") == permission_profile
            and self.status(target).get("drift_state") == "verified"
        )
        if same_bundle:
            effects = (
                "refresh approved provider provenance; exact native bundle is already verified",
            )
        elif bundle is not None:
            effects = tuple(
                f"write managed native file {item['path']}" for item in bundle.owned_files
            )
            effects = (*effects, f"write {self.config.state_name}")
        elif operation == "backup":
            effects = ("create target-bound provider backup",)
        elif operation == "restore":
            effects = (f"restore target-bound provider backup {getattr(args, 'backup_ref', None)}",)
        else:
            effects = ("remove provider-owned native files and provider state",)
        info = self.info()
        profile = info["projection_profile"]
        artifact, digest = v3.plan_artifact(
            provider_id=self.config.provider_id,
            provider_version=self.config.provider_version,
            provider_build_digest=str(info["provider_build_digest"]),
            provider_release_digest=str(getattr(args, "provider_release_digest", "")),
            operation_id=str(getattr(args, "operation_id", "")),
            operation=operation,
            canonical_target=target,
            expected_target_digest=self._target_digest(target),
            projection_profile_digest=str(profile["digest"]),
            bundle=bundle,
            backup_ref=getattr(args, "backup_ref", None),
            permission_profile=permission_profile,
            effects=effects,
            expires_at=expires_at,
        )
        answer = {
            "state": "planned",
            "plan": artifact,
            "plan_digest": digest,
            "expected_target_digest": artifact["expected_target_digest"],
            "effects": list(effects),
        }
        if bundle is not None:
            answer.update(bundle.echoes())
        return answer

    @contextlib.contextmanager
    def _lock(self, target: Path) -> Iterator[None]:
        held = self._require_target(target)
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (held.st_dev, held.st_ino):
                raise v3.ProtocolError(
                    "target_snapshot_invalid", "provider target changed during lock"
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise v3.ProtocolError(
                    "target_locked", "provider target is already locked"
                ) from exc
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _ensure_parent(target: Path, path: Path, created: list[Path]) -> None:
        try:
            relative = path.relative_to(target)
        except ValueError as exc:
            raise v3.ProtocolError("path_escapes_target", "provider path leaves target") from exc
        current = target
        for part in relative.parts[:-1]:
            current /= part
            held = Runtime._optional_lstat(current)
            if held is None:
                os.mkdir(current, OWNER_DIR_MODE)
                os.chmod(current, OWNER_DIR_MODE)
                created.append(current)
                continue
            if stat.S_ISLNK(held.st_mode) or not stat.S_ISDIR(held.st_mode):
                raise v3.ProtocolError("link_not_allowed", f"provider parent is unsafe: {current}")
            if hasattr(os, "geteuid") and held.st_uid != os.geteuid():
                raise v3.ProtocolError(
                    "target_snapshot_invalid",
                    f"provider parent has another owner: {current}",
                )

    def _write(
        self,
        target: Path,
        path: Path,
        snapshot: Snapshot,
        transaction: Path,
        created: list[Path],
    ) -> None:
        self._ensure_parent(target, path, created)
        if not snapshot.present:
            held = self._optional_lstat(path)
            if held is None:
                return
            if stat.S_ISLNK(held.st_mode) or not stat.S_ISREG(held.st_mode) or held.st_nlink != 1:
                raise v3.ProtocolError(
                    "link_not_allowed",
                    f"refusing to remove unsafe provider path: {path}",
                )
            path.unlink()
            return
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(transaction))
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(snapshot.content)
            os.chmod(temporary, snapshot.mode)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def _bundle_snapshots(self, bundle: v3.BundleIdentity) -> dict[str, Snapshot]:
        answer: dict[str, Snapshot] = {}
        with zipfile.ZipFile(bundle.path, "r") as archive:
            for item in bundle.owned_files:
                relative = self._safe_relative(item["path"])
                answer[relative] = Snapshot(
                    True, archive.read(f"files/{relative}"), int(item["mode"])
                )
        return answer

    def _backup_root(self, target: Path) -> Path:
        return target / self.config.backup_directory

    def _backup_pool(self, target: Path) -> Path:
        root = self._backup_root(target)
        held = self._optional_lstat(root)
        if held is None:
            os.mkdir(root, OWNER_DIR_MODE)
            os.chmod(root, OWNER_DIR_MODE)
            marker = {
                "format": "ai-stp-provider-backup-pool/3",
                "provider_id": self.config.provider_id,
                "canonical_target": self._target_key(target),
            }
            (root / "pool.json").write_bytes(self._json_bytes(marker))
            os.chmod(root / "pool.json", OWNER_FILE_MODE)
        elif stat.S_ISLNK(held.st_mode) or not stat.S_ISDIR(held.st_mode):
            raise v3.ProtocolError("backup_ref_invalid", "provider backup pool is unsafe")
        marker = self._read(root / "pool.json", owner_only=True)
        try:
            value = json.loads(marker.content) if marker.present else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise v3.ProtocolError(
                "backup_ref_invalid", "provider backup pool marker is invalid"
            ) from exc
        if value != {
            "format": "ai-stp-provider-backup-pool/3",
            "provider_id": self.config.provider_id,
            "canonical_target": self._target_key(target),
        }:
            raise v3.ProtocolError("backup_ref_invalid", "provider backup pool is not target-bound")
        return root

    def _create_backup(self, target: Path, paths: tuple[Path, ...]) -> str:
        root = self._backup_pool(target)
        token = secrets.token_hex(16)
        reference = f"backup:v3:{token}"
        slot = root / token
        os.mkdir(slot, OWNER_DIR_MODE)
        os.chmod(slot, OWNER_DIR_MODE)
        files = slot / "files"
        os.mkdir(files, OWNER_DIR_MODE)
        os.chmod(files, OWNER_DIR_MODE)
        entries: list[dict[str, Any]] = []
        for index, path in enumerate(paths):
            relative = self._safe_relative(path.relative_to(target).as_posix())
            snapshot = self._read(path, owner_only=(path == self._state_path(target)))
            entry: dict[str, Any] = {"path": relative, "present": snapshot.present}
            if snapshot.present:
                payload = files / f"{index:04d}"
                payload.write_bytes(snapshot.content)
                os.chmod(payload, OWNER_FILE_MODE)
                entry.update(
                    {
                        "payload": payload.name,
                        "digest": v3.raw_digest(snapshot.content),
                        "byte_length": len(snapshot.content),
                        "mode": snapshot.mode,
                    }
                )
            entries.append(entry)
        marker = {
            "format": "ai-stp-provider-backup/3",
            "provider_id": self.config.provider_id,
            "canonical_target": self._target_key(target),
            "backup_ref": reference,
            "entries": entries,
        }
        (slot / "backup.json").write_bytes(self._json_bytes(marker))
        os.chmod(slot / "backup.json", OWNER_FILE_MODE)
        self._prune_backups(root)
        return reference

    @staticmethod
    def _prune_backups(root: Path) -> None:
        slots = [item for item in root.iterdir() if item.name != "pool.json"]
        for item in slots:
            held = item.lstat()
            if stat.S_ISLNK(held.st_mode) or not stat.S_ISDIR(held.st_mode):
                raise v3.ProtocolError(
                    "backup_ref_invalid", "provider backup pool has an unsafe entry"
                )
        for item in sorted(slots, key=lambda path: path.stat().st_mtime_ns)[:-MAX_BACKUPS]:
            for child in sorted(item.rglob("*"), key=lambda path: len(path.parts), reverse=True):
                held = child.lstat()
                if stat.S_ISLNK(held.st_mode):
                    raise v3.ProtocolError("link_not_allowed", "provider backup contains a link")
                child.rmdir() if stat.S_ISDIR(held.st_mode) else child.unlink()
            item.rmdir()

    def _load_backup(self, target: Path, reference: str) -> tuple[Path, dict[str, Any]]:
        prefix = "backup:v3:"
        if not reference.startswith(prefix):
            raise v3.ProtocolError("backup_ref_invalid", "BackupRef format is invalid")
        token = reference[len(prefix) :]
        if len(token) != 32 or any(ch not in "0123456789abcdef" for ch in token):
            raise v3.ProtocolError("backup_ref_invalid", "BackupRef token is invalid")
        slot = self._backup_root(target) / token
        held = self._optional_lstat(slot)
        if held is None or stat.S_ISLNK(held.st_mode) or not stat.S_ISDIR(held.st_mode):
            raise v3.ProtocolError(
                "backup_ref_invalid", "BackupRef does not resolve to a safe slot"
            )
        marker = self._read(slot / "backup.json", owner_only=True)
        try:
            value = json.loads(marker.content) if marker.present else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise v3.ProtocolError("backup_ref_invalid", "BackupRef marker is invalid") from exc
        if (
            not isinstance(value, dict)
            or value.get("format") != "ai-stp-provider-backup/3"
            or value.get("provider_id") != self.config.provider_id
            or value.get("canonical_target") != self._target_key(target)
            or value.get("backup_ref") != reference
            or not isinstance(value.get("entries"), list)
        ):
            raise v3.ProtocolError("backup_ref_invalid", "BackupRef identity is invalid")
        return slot, value

    def _backup_snapshots(
        self, target: Path, slot: Path, marker: dict[str, Any]
    ) -> dict[Path, Snapshot]:
        answer: dict[Path, Snapshot] = {}
        for entry in marker["entries"]:
            if not isinstance(entry, dict):
                raise v3.ProtocolError("backup_ref_invalid", "BackupRef entry is invalid")
            path = target / self._safe_relative(entry.get("path"))
            if path in answer or not isinstance(entry.get("present"), bool):
                raise v3.ProtocolError(
                    "backup_ref_invalid", "BackupRef repeats or corrupts an entry"
                )
            if not entry["present"]:
                answer[path] = Snapshot(False)
                continue
            payload_name = entry.get("payload")
            if not isinstance(payload_name, str) or Path(payload_name).name != payload_name:
                raise v3.ProtocolError("backup_ref_invalid", "BackupRef payload is invalid")
            payload = self._read(slot / "files" / payload_name, owner_only=True)
            if (
                not payload.present
                or v3.raw_digest(payload.content) != entry.get("digest")
                or len(payload.content) != entry.get("byte_length")
                or entry.get("mode") not in {0o600, 0o644, 0o755}
            ):
                raise v3.ProtocolError("backup_ref_invalid", "BackupRef payload identity differs")
            answer[path] = Snapshot(True, payload.content, int(entry["mode"]))
        return answer

    def _plan_from_args(self, target: Path, args: Any) -> tuple[dict[str, Any], str]:
        raw = Path(str(getattr(args, "plan", "")))
        if not raw.is_absolute():
            raise v3.ProtocolError("plan_invalid", "provider plan path must be absolute")
        snapshot = self._read(raw)
        if not snapshot.present:
            raise v3.ProtocolError("plan_invalid", "provider plan is absent")
        value = v3.load_json_bytes(snapshot.content, "provider plan")
        if not isinstance(value, dict):
            raise v3.ProtocolError("plan_invalid", "provider plan must be an object")
        digest = str(getattr(args, "plan_digest", ""))
        if v3.canonical_digest("ai-stp:provider-plan:v3", value) != digest:
            raise v3.ProtocolError("plan_digest_mismatch", "provider plan digest differs")
        info = self.info()
        exact = {
            "format": v3.PLAN_FORMAT,
            "protocol_version": v3.PROTOCOL_VERSION,
            "provider_id": self.config.provider_id,
            "provider_version": self.config.provider_version,
            "provider_build_digest": info["provider_build_digest"],
            "provider_release_digest": str(getattr(args, "provider_release_digest", "")),
            "canonical_target": self._target_key(target),
            "platform": v3.canonical_platform(),
        }
        mismatches = [key for key, expected in exact.items() if value.get(key) != expected]
        if mismatches:
            raise v3.ProtocolError("plan_digest_mismatch", "provider plan identity differs")
        expires_at = value.get("expires_at")
        if not isinstance(expires_at, str):
            raise v3.ProtocolError("plan_expired", "provider plan has no expiry")
        try:
            expiry = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise v3.ProtocolError("plan_expired", "provider plan expiry is invalid") from exc
        if expiry.tzinfo is None or expiry <= dt.datetime.now(dt.timezone.utc):
            raise v3.ProtocolError("plan_expired", "provider plan has expired")
        if value.get("operation") not in self.config.operations:
            raise v3.ProtocolError(
                "unsupported_operation", "provider plan operation is unsupported"
            )
        profile = value.get("permission_profile")
        if profile is not None and profile not in self.config.permission_profiles:
            raise v3.ProtocolError(
                "unsupported_permission_profile", "provider plan profile is unsupported"
            )
        return value, digest

    def _state_payload(
        self,
        target: Path,
        plan: dict[str, Any],
        plan_digest: str,
        bundle: v3.BundleIdentity,
        backup_ref: str,
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        setup = bundle.manifest.get("setup")
        artifact = bundle.setup_passport.get("artifact")
        if not isinstance(setup, dict) or not isinstance(artifact, dict):
            raise v3.ProtocolError("digest_mismatch", "setup provenance is incomplete")
        state = {
            "state_schema": v3.STATE_FORMAT,
            "protocol_version": v3.PROTOCOL_VERSION,
            "provider_id": self.config.provider_id,
            "provider_version": self.config.provider_version,
            "provider_build_digest": plan["provider_build_digest"],
            "provider_release_digest": plan["provider_release_digest"],
            "harness_id": self.config.harness_id,
            "canonical_target": self._target_key(target),
            "target_identity_digest": "",
            "setup_version_passport_digest": str(setup.get("passport_digest", "")),
            "setup_definition_digest": str(artifact.get("digest", "")),
            "component_refs": list(bundle.components),
            "bundle_format": v3.BUNDLE_FORMAT,
            "bundle_digest": bundle.bundle_digest,
            "artifact_digest": bundle.artifact_digest,
            "projection_profile_digest": plan["projection_profile_digest"],
            "provider_plan_digest": plan_digest,
            "operation_id": plan["operation_id"],
            "target_precondition_digest": plan["expected_target_digest"],
            "permission_profile": plan.get("permission_profile"),
            "native_ownership": list(bundle.owned_files),
            "backup_ref": backup_ref,
            "previous_verified_identity": None
            if previous is None
            else previous.get("target_identity_digest"),
            "drift_state": "verified",
        }
        for key in (
            "setup_version_passport_digest",
            "setup_definition_digest",
            "bundle_digest",
            "artifact_digest",
            "projection_profile_digest",
            "provider_plan_digest",
            "target_precondition_digest",
        ):
            v3.require_digest(str(state[key]), key)
        return state

    def apply(self, target: Path, args: Any) -> dict[str, Any]:
        self._require_target(target)
        plan, plan_digest = self._plan_from_args(target, args)
        operation = str(plan["operation"])
        bundle: v3.BundleIdentity | None = None
        if operation in {"install", "replace"}:
            bundle = self._bundle(args)
            if plan.get("bundle") != bundle.echoes():
                raise v3.ProtocolError("plan_digest_mismatch", "bundle differs from provider plan")
        elif plan.get("bundle") is not None:
            raise v3.ProtocolError("plan_invalid", "non-bundle operation binds a bundle")
        with self._lock(target):
            if self._target_digest(target) != plan.get("expected_target_digest"):
                return {
                    "state": "stale",
                    "plan_digest": plan_digest,
                    "expected_target_digest": plan.get("expected_target_digest"),
                }
            current = self._state(target)
            native_content_changed = not (
                operation == "replace"
                and bundle is not None
                and current is not None
                and current.get("bundle_digest") == bundle.bundle_digest
                and current.get("artifact_digest") == bundle.artifact_digest
                and current.get("permission_profile") == plan.get("permission_profile")
                and self.status(target).get("drift_state") == "verified"
            )
            if operation == "install" and current is not None:
                raise v3.ProtocolError(
                    "target_already_managed", "install requires an unmanaged target"
                )
            if operation in {"replace", "backup", "remove"} and current is None:
                raise v3.ProtocolError(
                    "target_unmanaged", f"{operation} requires provider-managed state"
                )
            transaction = Path(tempfile.mkdtemp(prefix=".nddev-provider-txn-", dir=str(target)))
            os.chmod(transaction, OWNER_DIR_MODE)
            created: list[Path] = [transaction]
            old_paths = self._owned_paths(target, current)
            future = {} if bundle is None else self._bundle_snapshots(bundle)
            future_paths = tuple(target / relative for relative in future)
            restore: dict[Path, Snapshot] = {}
            if operation == "restore":
                reference = str(plan.get("backup_ref", ""))
                slot, marker = self._load_backup(target, reference)
                restore = self._backup_snapshots(target, slot, marker)
            all_paths = tuple(
                sorted(
                    set((*old_paths, *future_paths, *restore, self._state_path(target))),
                    key=str,
                )
            )
            before = {
                path: self._read(path, owner_only=(path == self._state_path(target)))
                for path in all_paths
            }
            backup_ref = ""
            recovery_ref: str | None = None
            try:
                if bundle is not None:
                    current_owned = {path.relative_to(target).as_posix() for path in old_paths}
                    for relative in future:
                        path = target / relative
                        if self._optional_lstat(path) is not None and relative not in current_owned:
                            raise v3.ProtocolError(
                                "native_ownership_conflict",
                                f"refusing to replace unmanaged native path: {relative}",
                            )
                backup_ref = self._create_backup(target, all_paths)
                if operation == "restore":
                    recovery_ref = backup_ref
                if bundle is not None:
                    future_names = set(future)
                    for path in old_paths:
                        if path.relative_to(target).as_posix() not in future_names:
                            self._write(target, path, Snapshot(False), transaction, created)
                    for relative, snapshot in future.items():
                        self._write(target, target / relative, snapshot, transaction, created)
                    state = self._state_payload(
                        target, plan, plan_digest, bundle, backup_ref, current
                    )
                    identity = dict(state)
                    identity.pop("target_identity_digest", None)
                    state["target_identity_digest"] = v3.canonical_digest(
                        f"{self.config.provider_id}:provider-target:v3",
                        {
                            "native": v3.target_digest(target, future_paths),
                            "state": identity,
                        },
                    )
                    self._write(
                        target,
                        self._state_path(target),
                        Snapshot(True, self._json_bytes(state), OWNER_FILE_MODE),
                        transaction,
                        created,
                    )
                elif operation == "remove":
                    for path in old_paths:
                        self._write(target, path, Snapshot(False), transaction, created)
                    self._write(
                        target,
                        self._state_path(target),
                        Snapshot(False),
                        transaction,
                        created,
                    )
                elif operation == "restore":
                    for path in all_paths:
                        self._write(
                            target,
                            path,
                            restore.get(path, Snapshot(False)),
                            transaction,
                            created,
                        )
                elif operation == "backup":
                    pass
            except BaseException:
                for path, snapshot in before.items():
                    self._write(target, path, snapshot, transaction, created)
                raise
            finally:
                with contextlib.suppress(OSError):
                    transaction.rmdir()
            self._cleanup_empty(target, (*old_paths, *future_paths))
        answer: dict[str, Any] = {
            "state": "verified",
            "operation": operation,
            "changed": native_content_changed if bundle is not None else operation != "backup",
            "plan_digest": plan_digest,
            "expected_target_digest": plan["expected_target_digest"],
            "backup_ref": str(plan.get("backup_ref")) if operation == "restore" else backup_ref,
        }
        if recovery_ref is not None:
            answer["recovery_backup_ref"] = recovery_ref
        if bundle is not None:
            answer.update(bundle.echoes())
        return answer

    @staticmethod
    def _cleanup_empty(target: Path, paths: tuple[Path, ...]) -> None:
        candidates: set[Path] = set()
        for path in paths:
            parent = path.parent
            while parent != target and parent.is_relative_to(target):
                candidates.add(parent)
                parent = parent.parent
        for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
            with contextlib.suppress(OSError):
                directory.rmdir()


def add_bundle_arguments(parser: Any, *, required: bool) -> None:
    parser.add_argument("--bundle", required=required)
    parser.add_argument("--bundle-format", required=required)
    parser.add_argument("--bundle-digest", required=required)
    parser.add_argument("--artifact-digest", required=required)
    parser.add_argument("--bundle-size", type=int, required=required)


def add_commands(subparsers: Any, add_target: Any, *, permission_profiles: bool) -> None:
    subparsers.add_parser("provider-info", help="Report provider protocol v3 capabilities.")
    validate = subparsers.add_parser("validate-bundle", help="Validate an exact HarnessBundle.")
    add_target(validate)
    add_bundle_arguments(validate, required=True)
    plan = subparsers.add_parser("plan-operation", help="Plan one provider v3 operation.")
    add_target(plan)
    plan.add_argument("--operation", required=True)
    plan.add_argument("--provider-release-digest", required=True)
    plan.add_argument("--operation-id", required=True)
    plan.add_argument("--expires-at", required=True)
    plan.add_argument("--backup-ref")
    if permission_profiles:
        plan.add_argument("--permission-profile")
    add_bundle_arguments(plan, required=False)
    apply = subparsers.add_parser("apply-operation", help="Apply one exact provider v3 plan.")
    add_target(apply)
    apply.add_argument("--plan", required=True)
    apply.add_argument("--plan-digest", required=True)
    apply.add_argument("--provider-release-digest", required=True)
    add_bundle_arguments(apply, required=False)
