#!/usr/bin/env python3
"""Validate packaged nddev-pi-app artifacts without runtime side effects."""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
CURRENT_MODULE_ID = "nddev-pi-app"
CURRENT_PACKAGE = "@earendil-works/pi-coding-agent"
CURRENT_REPOSITORY = "https://github.com/earendil-works/pi"
SETUP_IDS = ["nddev-builder"]
PROFILE_IDS = ["full-auto", "safe"]
MANAGED_FILES = [
    "agent/settings.json",
    "agent/AGENTS.md",
    "agent/skills/nddev-builder/SKILL.md",
    "agent/packages/nddev-builder/package.json",
    "agent/packages/nddev-builder/skills/nddev-builder/SKILL.md",
]
REQUIRED_VERSION_KEYS = {
    "build_version",
    "nddev_builder_package_version",
    "pi_package_bin",
    "pi_coding_agent_tested",
    "pi_command",
    "pi_node_requires",
    "pi_package_name",
    "pi_product_name",
    "pi_registry_integrity",
    "pi_registry_shasum",
    "pi_registry_tarball",
    "python_requires",
    "runtime_baseline_ref",
    "schema_version",
}
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
PACKAGE_ID_PATTERN = re.compile(r"@[A-Za-z0-9._-]+/pi-coding-agent")
REPOSITORY_PATTERN = re.compile(r"https://github\.com/[A-Za-z0-9._-]+/pi\b")
MODULE_ID_PATTERN = re.compile(r"nddev-[a-z0-9-]+-app")
SHARED_WORKFLOW_PIN = "2ccb80e96f5771b6a6b4eae63a4f47e232906dc7"


def read_json(relative: str, errors: list[str]) -> dict[str, Any] | None:
    path = ROOT / relative
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{relative}: unreadable or invalid JSON: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{relative}: top-level value must be an object")
        return None
    return value


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_identity(errors: list[str]) -> None:
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(ROOT)
        for match in PACKAGE_ID_PATTERN.finditer(content):
            require(
                match.group(0) == CURRENT_PACKAGE,
                f"{relative}: unexpected Pi package identity",
                errors,
            )
        for match in REPOSITORY_PATTERN.finditer(content):
            require(
                match.group(0) == CURRENT_REPOSITORY,
                f"{relative}: unexpected Pi repository identity",
                errors,
            )
        for match in MODULE_ID_PATTERN.finditer(content):
            require(
                match.group(0) == CURRENT_MODULE_ID,
                f"{relative}: cross-module id {match.group(0)!r}",
                errors,
            )


def validate_catalog(errors: list[str]) -> None:
    setup_dirs = sorted(path.name for path in (ROOT / "setups").iterdir() if path.is_dir())
    profile_dirs = sorted(path.name for path in (ROOT / "profiles").iterdir() if path.is_dir())
    require(setup_dirs == SETUP_IDS, "setups/: unexpected setup catalog", errors)
    require(
        profile_dirs == sorted(PROFILE_IDS),
        "profiles/: unexpected profile catalog",
        errors,
    )
    defaults: list[str] = []
    for profile_id in PROFILE_IDS:
        profile = read_json(f"profiles/{profile_id}/profile.json", errors)
        if profile is None:
            continue
        require(
            profile.get("id") == profile_id,
            f"profiles/{profile_id}: id mismatch",
            errors,
        )
        require(
            isinstance(profile.get("launch_args"), list),
            f"profiles/{profile_id}: launch_args missing",
            errors,
        )
        if profile.get("default") is True:
            defaults.append(profile_id)
    require(
        defaults == ["full-auto"],
        "profiles/: full-auto must be the only default",
        errors,
    )


def validate_contracts(errors: list[str]) -> None:
    version_text = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    version = read_json("build/version.json", errors)
    manifest = read_json("build/manifest.json", errors)
    contract = read_json("config/nddev-contract.json", errors)
    baseline = read_json("references/pi-baseline.json", errors)
    setup = read_json("setups/nddev-builder/setup.json", errors)
    settings = read_json("setups/nddev-builder/settings.json", errors)
    package = read_json("builder/nddev-builder/package.json", errors)
    if None in (version, manifest, contract, baseline, setup, settings, package):
        return
    assert version is not None
    assert manifest is not None
    assert contract is not None
    assert baseline is not None
    assert setup is not None
    assert settings is not None
    assert package is not None

    require(
        "verified_date" not in baseline,
        "baseline currentness observation must remain private",
        errors,
    )
    require(
        SEMVER.fullmatch(version_text) is not None,
        "VERSION: invalid semantic version",
        errors,
    )
    require(
        set(version) == REQUIRED_VERSION_KEYS,
        "build/version.json: key mismatch",
        errors,
    )
    require(version.get("build_version") == version_text, "build version mismatch", errors)
    require(
        manifest.get("build_version") == version_text,
        "manifest version mismatch",
        errors,
    )
    require(
        package.get("version") == version.get("nddev_builder_package_version"),
        "builder package version mismatch",
        errors,
    )
    runtime_version = version.get("pi_coding_agent_tested")
    require(
        baseline.get("package", {}).get("version") == runtime_version,
        "runtime baseline version mismatch",
        errors,
    )
    require(
        contract.get("software", {}).get("current_version") == runtime_version,
        "contract runtime version mismatch",
        errors,
    )
    require(
        contract.get("software", {}).get("package") == CURRENT_PACKAGE,
        "contract package identity mismatch",
        errors,
    )
    require(
        baseline.get("package", {}).get("name") == CURRENT_PACKAGE,
        "baseline package identity mismatch",
        errors,
    )
    require(
        baseline.get("product", {}).get("repository") == CURRENT_REPOSITORY,
        "baseline repository mismatch",
        errors,
    )
    require(
        contract.get("setup_system", {}).get("setup_ids") == SETUP_IDS,
        "contract setup ids mismatch",
        errors,
    )
    require(
        contract.get("setup_system", {}).get("profile_ids") == PROFILE_IDS,
        "contract profile ids mismatch",
        errors,
    )
    require(setup.get("id") == "nddev-builder", "setup id mismatch", errors)
    require(
        setup.get("managed_files") == MANAGED_FILES,
        "setup managed files mismatch",
        errors,
    )
    require(
        settings.get("skills") == [],
        "settings source skills must be projected by the manager",
        errors,
    )
    require(
        settings.get("packages") == [],
        "settings source packages must be projected by the manager",
        errors,
    )
    require(
        package.get("pi", {}).get("skills") == ["skills/nddev-builder"],
        "builder package Pi skill manifest mismatch",
        errors,
    )
    registry = baseline.get("package", {})
    require(
        registry.get("integrity") == version.get("pi_registry_integrity"),
        "registry integrity mismatch",
        errors,
    )
    require(
        registry.get("shasum") == version.get("pi_registry_shasum"),
        "registry shasum mismatch",
        errors,
    )
    require(
        registry.get("tarball") == version.get("pi_registry_tarball"),
        "registry tarball mismatch",
        errors,
    )


def validate_release_and_runtime_integrity(errors: list[str]) -> None:
    release = (ROOT / "release/package.yml").read_text(encoding="utf-8")
    require(
        "package_name: nddev-pi-app" in release,
        "release package identity mismatch",
        errors,
    )
    required_roots = {
        "README.md",
        "LICENSE",
        "VERSION",
        "AGENTS.md",
        ".claude",
        "build",
        "builder",
        "cli-tools",
        "config",
        "docs",
        "profiles",
        "references",
        "setups",
    }
    require(
        required_roots.issubset(set(release.split())),
        "release package membership is incomplete",
        errors,
    )
    manager = (ROOT / "cli-tools/nddev_pi.py").read_text(encoding="utf-8")
    for fragment in (
        "NPM_JSON_OUTPUT_MAX_BYTES",
        "cold_product_namespace_snapshot",
        "recover_anchor_publication_alias",
        "cleanup_pending",
        "verify_tarball_identity",
        "require_safe_launch_args",
        "workspace",
        "PI_CODING_AGENT_DIR",
    ):
        require(
            fragment in manager,
            f"manager runtime-integrity fragment missing: {fragment}",
            errors,
        )
    for fragment in ("plugin_marketplace", "marketplace.json", "default target"):
        require(
            fragment not in manager,
            f"manager contains unsupported surface: {fragment}",
            errors,
        )


def validate_public_instruction_surface(errors: list[str]) -> None:
    claude = ROOT / ".claude"
    try:
        claude_info = claude.lstat()
    except OSError:
        claude_info = None
    require(
        claude_info is not None
        and claude_info.st_mode & 0o170000 == 0o040000
        and not claude.is_symlink(),
        ".claude must be a real directory",
        errors,
    )
    if claude_info is not None and claude_info.st_mode & 0o170000 == 0o040000:
        require(
            sorted(path.name for path in claude.iterdir()) == ["CLAUDE.md"],
            ".claude must contain exactly CLAUDE.md",
            errors,
        )
    for relative in ("AGENTS.md", ".claude/CLAUDE.md"):
        path = ROOT / relative
        try:
            info = path.lstat()
        except OSError:
            info = None
        require(
            info is not None and info.st_mode & 0o170000 == 0o100000 and not path.is_symlink(),
            f"{relative} must be a real file",
            errors,
        )
    bridge = ROOT / ".claude/CLAUDE.md"
    if bridge.is_file() and not bridge.is_symlink():
        require(
            bridge.read_bytes() == b"@../AGENTS.md\n",
            "Claude bridge is invalid",
            errors,
        )


def validate_provider_protocol(errors: list[str]) -> None:
    require(
        stat.S_IMODE((ROOT / "cli-tools/nddev_pi.py").stat().st_mode) == 0o755,
        "provider manager must be executable with mode 0755",
        errors,
    )
    contract = read_json("config/nddev-contract.json", errors)
    manifest = read_json("build/manifest.json", errors)
    expected_commands = [
        "provider-info",
        "validate-bundle",
        "plan-operation",
        "apply-operation",
        "recover-operation",
        "status",
    ]
    for label, document in (("contract", contract), ("manifest", manifest)):
        provider = document.get("provider_protocol") if isinstance(document, dict) else None
        require(
            isinstance(provider, dict),
            f"{label}: provider_protocol is required",
            errors,
        )
        if not isinstance(provider, dict):
            continue
        require(provider.get("version") == 3, f"{label}: provider version mismatch", errors)
        require(
            provider.get("bundle_format") == "ai-stp-bundle/1",
            f"{label}: provider bundle format mismatch",
            errors,
        )
        require(
            provider.get("commands") == expected_commands,
            f"{label}: commands mismatch",
            errors,
        )
    for relative in (
        "cli-tools/provider_protocol_v3.py",
        "cli-tools/provider_runtime_v3.py",
    ):
        require(
            (ROOT / relative).is_file(),
            f"provider runtime file missing: {relative}",
            errors,
        )
    workflows = ROOT / ".github" / "workflows"
    require(
        workflows.is_dir(),
        "required release-check workflow directory is missing",
        errors,
    )
    if workflows.is_dir():
        workflow_files = {path.name for path in workflows.iterdir() if path.is_file()}
        require(
            workflow_files == {"test.yml"},
            "public repository may contain only the release-check test.yml workflow",
            errors,
        )
        if workflow_files == {"test.yml"}:
            workflow = (workflows / "test.yml").read_text(encoding="utf-8")
            for fragment in (
                "permissions:\n  contents: read",
                "runs-on: ubuntu-24.04",
                "name: test",
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "run: python3 cli-tools/validate_public_contracts.py",
            ):
                require(
                    fragment in workflow,
                    f"test.yml is missing required release-check fragment: {fragment!r}",
                    errors,
                )
            require(
                "pull_request_target" not in workflow and "${{ secrets" not in workflow,
                "test.yml may not use privileged PR triggers or repository secrets",
                errors,
            )


def main() -> int:
    errors: list[str] = []
    validate_identity(errors)
    validate_catalog(errors)
    validate_contracts(errors)
    validate_release_and_runtime_integrity(errors)
    validate_public_instruction_surface(errors)
    validate_provider_protocol(errors)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
