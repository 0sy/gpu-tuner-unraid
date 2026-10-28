# Unraid release candidate

First beta image tag: `1.3.1-unraid.1-beta.1`, based on upstream GPU Tuner 1.3.1.

The fork adds Unraid Docker packaging, persistent appdata, a password login page, independent browser sessions, remembered logins across container recreation, password-reset command and recovery guidance, light/dark mode controls and a Community Applications template. It preserves upstream MIT attribution.

## Prepared artifacts

- `container/Dockerfile`: NVIDIA-enabled container image with root daemon and unprivileged WebUI.
- `templates/gpu-tuner.xml`: Community Applications template pointing to the intended public GHCR image.
- `ca_profile.xml`: repository metadata.
- `icon.svg`: vector source; `icon.png`: transparent 256-pixel Unraid icon. Publish both; templates use the PNG.
- `.github/workflows/tests.yml`: container-based Unraid, installed-image and offline upstream checks.
- `.github/workflows/publish.yml`: manually dispatched build/test/push pipeline. No automatic publishing on commit or tag creation.
- `docs/unraid.md`: installation, credential recovery, remembered-login behavior and old-layout migration.

## Remaining release gates

1. Add Container deployment completed using `gpu-tuner-ca-test`, separate appdata and port 18766; user-provided logs confirmed all three GPUs and the WebUI started. Manual WebUI authentication, reset, theme, remembered login and container-menu access checks passed.
2. Final changes reviewed; user authorized commit/push and image publication on 2026-10-28. Destination: `0sy/gpu-tuner-unraid` only.
3. Dispatch the publish workflow with the approved version. Make the GHCR package public and verify an unauthenticated pull.
4. Run `python3 tests/check_unraid_assets.py --public` successfully (also enforced by the publish workflow), then run the Community Applications submission portal Validate and Scan checks.
5. Submit the beta listing for review after scan findings are resolved.

The local template is installed at `/boot/config/plugins/dockerMan/templates-user/my-gpu-tuner-ca-test.xml`. Its mandatory initial-password field is intentionally empty. Set it in the Unraid form before Apply. After successful login, remove the variable entirely in Advanced View and Apply again; an empty required field cannot be saved. The preview `gpu-tuner-install-test` and older development controller remain stopped; the user-installed `gpu-tuner-ca-test` is the active controller.

## Local release checks

The old-layout migration copy passed a live GPU-daemon startup while preserving original data. The 130 offline daemon/fleet/demo tests passed after aligning fan-mode fixtures with the existing Unraid default. The installed-image first-run/reset smoke check passed, including private root-owned appdata parents. All 62 Unraid regression tests passed, covering authentication, renamed-container recovery commands and password input validation (192 automated tests total). See `docs/unraid.md` for exact results and remaining manual checks.
