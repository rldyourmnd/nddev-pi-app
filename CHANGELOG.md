# Changelog

## [0.2.0] - 2026-08-09

- Added the capability-negotiated public provider protocol v3 with exact
  HarnessBundle validation, pure planning, exact-digest application, status,
  backup, restore, and ownership-scoped removal.
- Preserve exact component/setup/bundle/plan provenance through Pi-native
  settings, instruction, skill, and local-package projections; MCP, hooks,
  commands, and agents remain fail-closed unsupported surfaces.
- Updated launch documentation to bind its workspace-scope rationale to the
  current tested Pi Coding Agent 0.84.1 source.

## [0.1.2] - 2026-08-08

- Update Pi Coding Agent to `0.84.1` with its exact npm SRI, shasum, and
  tarball identity.
- Revalidate the unchanged Node `>=22.19.0` floor, `pi` entrypoint, package
  layout, wrapper version identity, and target-owned install lifecycle; the
  package-family dependency set advances coherently to `0.84.1`.

## [0.1.1] - 2026-08-06

- Update Pi Coding Agent to `0.84.0`, including exact npm integrity and tarball
  identity.
- Keep the native installed-wrapper `pi --version` output (`0.0.0`) paired with
  package.json `0.84.0` as two independently required executable identities.
- Keep persistent external lock anchors valid across manager version upgrades;
  legacy version-bearing bindings are accepted only when every stable identity
  field still matches.

## [0.1.0]

- Add an explicit-target Pi Coding Agent setup manager.
- Add the nddev-builder content setup with full-auto and safe runtime profiles.
- Project nddev-builder through documented Pi skills/package surfaces.
- Add public contract metadata, Pi baseline evidence, and release CI callers.
