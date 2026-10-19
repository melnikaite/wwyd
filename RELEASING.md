# Releasing wwyd

A pushed tag `vX.Y.Z` runs [`.github/workflows/release.yml`](.github/workflows/release.yml):
lint and tests, a check that the tag equals the `version` in `pyproject.toml`, the sandbox
assets (`scripts/build_sandbox.py`: downloads the sha256-pinned CPython-for-WASI archive and
builds `src/wwyd/sandbox/assets/`), `uv build` with a check that the wheel contains them,
upload to PyPI (Trusted Publishing, no token), a multi-arch image
`ghcr.io/melnikaite/wwyd` (linux/amd64, linux/arm64; the assets are built in the job
before `docker build`, so `COPY src` includes them; tags `X.Y.Z`, `X.Y`, `latest`), and a
GitHub Release whose body is the `## [X.Y.Z]` section of `CHANGELOG.md`, with the sdist
and wheel attached.

## One-time setup (by hand)

1. **PyPI pending trusted publisher.** Sign in at https://pypi.org, open
   *Your account → Publishing → Add a new pending publisher → GitHub*, and enter:
   - PyPI project name: `wwyd`
   - Owner: `melnikaite`
   - Repository name: `wwyd`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   The project is created on the first successful upload; the pending publisher then
   becomes a normal one.
2. **GitHub environment.** In the repository, *Settings → Environments → New environment*,
   name it `pypi`. Optionally add yourself as a required reviewer (each release then waits
   for your approval before uploading) and restrict deployment to tags `v*`.
3. **GHCR visibility.** After the first release has pushed the image, open
   https://github.com/users/melnikaite/packages/container/package/wwyd →
   *Package settings → Change visibility → Public*, and check that the package is linked
   to the `wwyd` repository (the image's `org.opencontainers.image.source` label does this).
4. **Workflow permissions.** *Settings → Actions → General → Workflow permissions*: the
   workflow requests what it needs per job (`id-token`, `packages`, `contents: write`); the
   default "read repository contents" setting is enough.

## Per release

1. Bump `version` in `pyproject.toml` (`uv version X.Y.Z` also updates `uv.lock`).
2. In `CHANGELOG.md`, move the entries under `## [Unreleased]` into
   `## [X.Y.Z] - YYYY-MM-DD` (today's date), and update the compare links at the bottom.
3. Bump the pinned image version in `deploy/docker-compose.yml`,
   `deploy/cloud-init.yaml`, `docs/install.md` and `README.md`.
4. Check locally: `uv run python -I scripts/build_sandbox.py` (once; the assets are
   gitignored), `uv run ruff check . && uv run ruff format --check . && uv run pytest`,
   and `uv build` (inspect `dist/` if packaging changed; the wheel must contain
   `wwyd/sandbox/assets/python.wasm` and `lib/python315.zip`, ~3 MB compressed).
   Moving to a new upstream CPython-for-WASI release means updating `UPSTREAM_*` and
   `UPSTREAM_SHA256` in `scripts/build_sandbox.py` together.
5. Commit (`Release vX.Y.Z`) and push to `main`; wait for CI to pass.
6. Create a signed tag and push it:

   ```sh
   git tag -s vX.Y.Z -m "wwyd vX.Y.Z"
   git push origin vX.Y.Z
   ```

7. Watch the *Release* workflow (approve the `pypi` environment if you added a reviewer).
   Afterwards check https://pypi.org/project/wwyd/, `uvx wwyd@X.Y.Z --version`,
   `docker run --rm ghcr.io/melnikaite/wwyd:X.Y.Z wwyd --version`, and the GitHub Release.

If the workflow fails before the PyPI upload, fix the problem, delete the tag
(`git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`) and tag again. A version that
reached PyPI can never be uploaded again: release the fix as the next patch version.
