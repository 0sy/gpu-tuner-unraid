# GPU Tuner on Unraid

This fork packages the upstream NVIDIA GPU controller with a password-authenticated LAN WebUI. Unraid functionality has been exercised in the development deployment. The distributable installation flow is still being validated; it is not published in Community Applications yet.

## Requirements and template settings

- Unraid with Docker enabled, the NVIDIA Driver plugin, and a supported NVIDIA GPU visible in `nvidia-smi`. A GPU assigned exclusively to a VM is unavailable to this container.
- A dedicated writable appdata directory mapped to `/data`. Default: `/mnt/user/appdata/gpu-tuner`. Do not use the same directory for two running instances. Back it up before upgrades.
- Bridge networking with container TCP port 8765 mapped to an unused host port.
- `GPU_TUNER_PUBLIC_URL` must match the exact URL used in your browser, for example `http://192.168.1.10:8765`. If you change the host port, update this URL too. The distributed CA template intentionally leaves both Value and Default empty because each server has a different address. The local test template was prefilled for this development host only; release checks reject a populated WebUI URL or initial-password default.
- An initial password of 12–1024 characters, entered in the mandatory masked template field. Unraid blocks Apply while this required field is empty. There is no shared default password.

The template grants `--gpus all` and `--cap-add=SYS_ADMIN`, matching the tested GPU access configuration. It does not enable privileged mode or mount host system directories. GPU control requires substantial privilege; use this app only on a trusted LAN. The UI drops to UID/GID 10001 while the GPU daemon runs as root. Some GPU models expose monitoring but no fan or clock controls.

Bridge networking has passed the live Docker installation checks. The existing development deployment used a custom `br0` address. For custom networking, choose an unused container IP in Unraid and set the WebUI URL to that container's address and port 8765. Do not use host networking: startup expects one non-loopback container IPv4 address.

## Test before publication

1. Build the checkout locally:

   ```bash
   docker build -f container/Dockerfile -t gpu-tuner-unraid:packaging-test .
   ```

2. In the Unraid Docker tab, enable Advanced View and template authoring mode if necessary. Import `templates/gpu-tuner.xml` as a user template named `my-gpu-tuner-test.xml` in `/boot/config/plugins/dockerMan/templates-user/`. This is an explicit host-side installation step; the repository's tests do not write to the boot device.
3. In Add Container, select the imported template. Before Apply, change Name to `gpu-tuner-test`, Repository to `gpu-tuner-unraid:packaging-test`, appdata to a new empty directory such as `/mnt/user/appdata/gpu-tuner-install-test`, and host port to an unused value such as 18765. Set WebUI URL to `http://YOUR-UNRAID-IP:18765` and supply the initial password. Retain the GPU extra parameters and 45-second stop timeout.
4. Stop the development container before starting the test container. Never run two controllers against the same GPU. Confirm the WebUI link opens the login page; sign in and check GPU telemetry and available controls. Exercise only changes you intend to apply to real hardware.
5. Enable Advanced View in the container editor, use **Remove** on the **Initial WebUI Password** variable, and Apply. Remove the variable entirely rather than blanking its mandatory field. Sign in with the same password again. Initialization preserves the stored hash; it does not reset the password on container recreation. Docker configuration and saved template XML can retain the original plaintext until you remove it. Remove any installation command logs or backups containing it as appropriate.
6. Set a recognizable, safe setting through the UI. Record it, stop/start the container, then recreate it through Edit/Apply using the same appdata directory. Verify the setting, password, and WebUI link survive. Check shutdown logs for normal daemon termination and fan restoration. A 45-second stop timeout allows supervised shutdown and its fan-restoration attempt.
7. For an upgrade test, back up appdata while stopped, then recreate with the candidate image and the same appdata mapping. Check login, persisted controls and telemetry. Verify Remember me stays signed in after recreation; ordinary sessions require signing in again.
8. Stop and remove the test container. Keep or explicitly delete its dedicated test appdata. Restart the development container if desired. Removing a container should not erase the persistent appdata.

Do not apply the supplied production template unchanged before publication: its GHCR image and raw template/icon URLs refer to the intended public release and may not exist yet.

## Migrate the older development layout

This is only needed for the early development container that mounted `config/` at `/etc/gpu-tuner`, `state/` at `/var/lib/gpu-tuner` and `ui/` at `/home/gputuner/.config/gpu-tuner`. New installations already use the packaged layout.

Stop the old controller and back up its appdata. Create a separate destination so the original remains available for rollback:

```bash
(
  set -eu
  migration_source=/mnt/user/appdata/gpu-tuner
  migration_destination=/mnt/user/appdata/gpu-tuner-migrated
  test ! -e "$migration_destination"
  mkdir -p "$migration_destination/ui/.config"
  cp -a "$migration_source/config" "$migration_source/state" "$migration_destination/"
  cp -a "$migration_source/ui" "$migration_destination/ui/.config/gpu-tuner"
)
```

Map the new destination to `/data` in the candidate template. Preserve the GPU parameters and set the correct WebUI URL. If the old deployment has no `password.hash`, supply an Initial WebUI Password for its first password-enabled start; an existing hash is preserved. After successful login, enable Advanced View, remove the Initial WebUI Password variable entirely and Apply.

Verify the saved GPU budget/settings, telemetry, password login and reset. Keep the original data and container stopped for rollback; never run both controllers at once. To roll back, stop the new container before restarting the old one.

A copy of the actual development data was checked for valid config/state, preserved desktop token and UI permissions. The original files were not modified. New appdata initialization normalizes `.config` ownership for UID 10001, including when a root shell created its parent with mode 0700.

## Local automated checks

These create disposable containers and never start the GPU daemon or request a GPU:

```bash
docker run --rm --network none --entrypoint python \
  --mount type=bind,src="$PWD",dst=/workspace,readonly \
  --workdir /workspace gpu-tuner-unraid:packaging-test \
  -B -m unittest discover -s tests -p 'test_unraid_*.py' -v

docker run --rm --network none --tmpfs /data --entrypoint python \
  --mount type=bind,src="$PWD/tests/container_install_smoke.py",dst=/checks/install.py,readonly \
  gpu-tuner-unraid:packaging-test -B /checks/install.py
```

The installed-image smoke check covers empty-data initialization, missing initial password rejection, file ownership/modes, preserving settings and password on reinitialization, unprivileged HTTP login, installed dashboard assets, interactive password reset and logout. It deliberately does not validate NVIDIA runtime injection, GPU writes, supervisor shutdown or the Unraid Add Container UI; complete the manual flow above before release.

## Persistent files

Under appdata:

| Path | Purpose |
| --- | --- |
| `config/config.json` | Daemon configuration and GPU budget |
| `state/state.json` | Saved GPU controls |
| `ui/.config/gpu-tuner/password.hash` | WebUI password hash |
| `ui/.config/gpu-tuner/browser-sessions.json` | Remembered browser token digests |
| `ui/.config/gpu-tuner/token` | Separate desktop authentication token |

## Container icon

`icon.svg` is the editable vector source; `icon.png` is a transparent 256 × 256 export used by the template and repository profile. On the tested Unraid 7.3.4 installation, Docker Manager downloaded the SVG verbatim into a `.png` cache file, producing a format mismatch. Use the PNG URL for compatibility:

```xml
<Icon>https://raw.githubusercontent.com/0sy/gpu-tuner-unraid/main/icon.png</Icon>
```

Push `icon.png`, the template and profile before publishing the image. CI runs `python3 tests/check_unraid_assets.py` to check PNG dimensions/checksums and both Icon URLs. The manual publish workflow runs `python3 tests/check_unraid_assets.py --public` before building or pushing: anonymous downloads must return the same valid PNG with Content-Type `image/png`, and the public template/profile must reference it. A missing, outdated, private or incorrectly typed asset blocks image publication. The local test container's persistent and RAM icon caches were replaced with the PNG; refresh the Docker page to reload it. No container restart is required for that cache update.

## Remembered browser login

Select **Remember me** when signing in to stay signed in until logout or password reset, with no server-side expiry. Remembered sessions survive container restart and recreation with the same appdata. Regular sessions remain in memory, expire after 12 hours, and use browser-session cookies.

Only digests of the random session token and credential are saved in `ui/.config/gpu-tuner/browser-sessions.json` (mode 0600). The browser receives an HttpOnly, SameSite=Strict cookie containing the random token, never the password. The existing capacity limit of 32 sessions still applies; issuing more sessions evicts the oldest.

Browsers impose cookie lifetime limits. The app sets a 400-day cookie and renews it on authenticated requests, so regular use keeps it alive. Clearing browser data, browser privacy policies, or leaving the app unused beyond the browser's cookie lifetime can require signing in again. See [Chrome's cookie lifetime guidance](https://developer.chrome.com/blog/cookie-max-age-expires/).

The login page's **Forgot password?** section provides the administrator reset command and a **Copy command** button. On Unraid, the command uses `HOST_CONTAINERNAME` supplied by Docker Manager, so renamed containers receive the correct command. Paste it into Unraid Terminal to reset the password; it does not execute a privileged command from an unauthenticated browser.

## Reset the WebUI password

With the container running, open **Terminal** in the Unraid WebGUI and run:

```bash
docker exec -it gpu-tuner python /usr/local/bin/gpu-tuner-entrypoint --reset-password
```

Replace `gpu-tuner` with your container name if different (for example, `gpu-tuner-test`).

1. Enter a new password of 12–1024 characters at the prompt. Input is hidden.
2. Enter the same password again to confirm.
3. Wait for `WebUI password reset. Sign in with the new password; no restart needed.`
4. Open the WebUI and sign in with the new password.

The current password is not required; access to the Unraid terminal authorizes recovery. The new password is not placed in shell history or Docker environment variables. No container restart, file deletion or template edit is needed. Existing browser sessions are rejected on their next authenticated request. GPU settings and the separate desktop token are preserved. Invalid input or a confirmation mismatch leaves the current password unchanged.

This command requires an image built with the reset command; it is not available in the older development image until upgraded. The login page provides a Forgot password? command-copy panel; password reset runs in the Unraid terminal.

### Offline recovery fallback

If the container cannot start, the offline fallback is to stop it, back up appdata, remove only `ui/.config/gpu-tuner/password.hash`, then set a new Initial WebUI Password and Apply. Sign in, then remove the initial-password variable in Advanced View and Apply again. Setting the initial-password variable alone never replaces an existing credential.

## Login troubleshooting

If login fails with HTTP 421 or 403, verify the browser address matches WebUI URL, including the port. A missing/invalid hash causes password login to fail closed. Check container logs for startup failures. Do not publish password hashes or tokens in bug reports.

## Local validation record (2026-10-28)

Local host: **Unraid 7.3.4** (user-reported), NVIDIA driver **580.196.07**, three **GeForce RTX 3060** GPUs. Live Docker installation-flow checks completed; the user installed the CA test template through the Unraid form and supplied successful WebUI/three-GPU startup logs.

- Built `gpu-tuner-unraid:packaging-test`, image ID `sha256:61167691e7dc772eee6bda88b3d935fff6f900ac1c33be9ee1d43badbe61ac1b`.
- All 62 Unraid regression tests passed in the final candidate, including persistent remembered sessions, ordinary-session expiry, cookie renewal, capacity eviction, store corruption/write failure, logout and password-reset invalidation. Recovery command copying passed secure-clipboard, HTTP fallback and manual-copy checks.
- Installed-image first-run and password-recovery smoke tests passed without GPU access. Recovery rejected missing/weak replacement passwords, accepted the new password through HTTP, rejected the old password, preserved config/state byte-for-byte, and retained credential ownership/mode on reinitialization.
- XML parsed successfully; the installed Unraid `xmlToCommand` parser generated bridge networking, TCP port mapping, appdata and GPU parameters without executing the command or creating paths.
- Python syntax checks and `git diff --check` passed.
- Live fresh installation with isolated appdata and bridge port 18765 passed: protected dashboard, login and real GPU telemetry. Docker settings matched the candidate template; the Unraid Add Container form itself was not exercised.
- The actual interactive reset command passed through a terminal: password input remained hidden, the old password/session failed, the new password worked, and GPU config/state were unchanged.
- Stop/start and container recreation without the initial-password environment variable preserved the reset password and config/state byte-for-byte.
- Three candidate shutdowns exited 0 and logged supervisor fan restoration without a reported failure. Physical fan behavior was not independently instrumented during shutdown.
- Fixed a first-install failure when appdata starts with mode 0700: initialization now sets the `/data` mount root to 0711 so the unprivileged UI can traverse it; config/state and UI child directories retain private permissions. Added this scenario to the installed-image smoke check.
- The test container was removed and the development controller restarted. All three GPU power limits remained at their original 130 W. Isolated test appdata was retained at `/mnt/user/appdata/gpu-tuner-install-test`, `-2` and `-3`; it contains test credentials/settings only.
- Latest preview check: a remembered HTTP login survived an actual candidate restart, received a renewed cookie and was rejected after logout. The Forgot password panel is served. The preview on host port 18765 was subsequently stopped for the Add Container form test; the development controller remains stopped.
- Old-layout migration passed on a copy of the actual development data, including a live GPU-daemon start and authentication with the preserved desktop token. Original config/state/UI files remained unchanged. Fixed initialization to assign the migrated `.config` parent to UID 10001 with mode 0700.
- The offline suite passed all 75 daemon/safety tests, 38 fleet tests and 17 demo tests. Five inherited test expectations were aligned with the already-committed Unraid driver-auto fan default; no daemon behavior was changed. Systemd installer dry-run checks were skipped in the GPU-free test container because it has no `nvidia-smi`.
- Final review corrected login validation to reject actual NUL characters while accepting a valid password containing a literal backslash escape; a regression covers both cases.
- The local Add Container template is installed at `/boot/config/plugins/dockerMan/templates-user/my-gpu-tuner-ca-test.xml`, using candidate image `gpu-tuner-unraid:packaging-test`, separate appdata `/mnt/user/appdata/gpu-tuner-ca-test` and port 18766. The installed Unraid parser accepted it. The user subsequently submitted the form and confirmed startup through container logs.
- No image or repository changes were published.

## Manual WebUI validation (2026-10-28)

The user confirmed these checks passed in the running Unraid test WebUI:

- Interactive password reset using the command provided by the recovery panel.
- Light/dark mode switching.
- Remember me retaining login across browser close/reopen.
- Sign out returning to the login page.
- Opening the WebUI from the Unraid Docker container context menu.

## Release gates

- Complete the fresh-install, restart, recreate, upgrade and shutdown checks above on Unraid. Record the Unraid version, NVIDIA driver, GPU model, image ID and results.
- Run CI and inspect the final diff. Preserve upstream MIT attribution.
- After approval, publish the tested image to `ghcr.io/0sy/gpu-tuner-unraid` with a version tag and `latest`, and make the package publicly pullable. Verify an unauthenticated pull; keep the previous version available for rollback. The manual **Publish Unraid image** workflow in `.github/workflows/publish.yml` builds and tests the image before pushing versioned and `latest` tags. It runs only when explicitly dispatched. After the first push, check package visibility in GitHub and set it to public before testing an unauthenticated pull.
- After approval, push the template, icon, profile and installation guide. Verify their raw URLs and support destination. Keep the beta marker until installation testing supports removing it.
- Run Validate and Scan in the Community Applications submission portal, address their findings, then submit for review. No catalog submission is performed by local checks.

Current reference material: [Community Applications repository XML](https://ca.unraid.net/submit/help/repository-xml), [repository profile](https://ca.unraid.net/submit/help/repository-info-xml), and [submission help](https://ca.unraid.net/submit/help).
