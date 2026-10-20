# Prebuilt toolchain image for `ddc job` (optional)

Without it, every job lease runs `apt-get update && apt-get install` for sshd, rsync, aria2 and whatever you list under `[image].apt`. With
it, `jobremote/boot.sh` finds everything already installed (`dpkg -s`) and goes straight to sshd. If the prebuilt image cannot be pulled or
does not boot, `ddc job` closes that lease and redeploys on the stock image, so publishing one is never a requirement and a broken tag is
never fatal. (This image is for job leases only; the dev box and workers use the controller's `image` setting.)

How much it saves is workload and provider dependent and was **not measured**: the apt share of a boot is unknown (the simulation assumes a
share, ESTIMATED), and a boot that is already fast saves only seconds. Expect more benefit on a cold provider and with many packages; the telemetry's `boot.apt_installed` and `container_start_after_lease_s` fields show the
real numbers after the first runs.

## Build

Pick a base that matches the hardware. The base must be Debian/Ubuntu based (apt/dpkg) and, for GPU jobs, contain the runtime libraries your
workload needs (the driver itself comes from the Akash host):

```bash
# CPU
docker build -f docker/Dockerfile -t ghcr.io/OWNER/ddc-base:cpu-1 .
# GPU: a base image that already carries your GPU userland
docker build -f docker/Dockerfile --build-arg BASE=YOUR-GPU-BASE-IMAGE:TAG \
  --build-arg EXTRA_APT="libgl1" -t ghcr.io/OWNER/ddc-base:gpu-1 .
```

A complete GPU example is `workflows/comfyui/Dockerfile` (ComfyUI at a pinned commit, its custom nodes and small weights on a PyTorch base pinned by digest; the model weights stay in the spec's `[cache]`):
see [workflows/README.md](../workflows/README.md#less-setup-prebuilt-environment-cached-weights).

Everything listed in a spec's `[image].apt` (plus `openssh-server rsync curl ca-certificates aria2 tmux`) should be in the image; anything
missing is simply installed at start. Bake in tools and system libraries. Do **not** bake secrets, API keys, tokens, deploy keys or SSH host
keys: the Dockerfile deletes the host keys and `boot.sh` generates fresh ones per container, and the Dockerfile contains no credentials or
build secrets. Large model weights are better served by the `[cache]` mechanism than by a multi-tens-of-GB image.

## Publish

```bash
echo "$REGISTRY_TOKEN" | docker login ghcr.io -u YOUR-USER --password-stdin
docker push ghcr.io/OWNER/ddc-base:gpu-1
```

Make the package public (providers pull anonymously), then reference it in the spec:

```toml
[image]
prebuilt = "ghcr.io/OWNER/ddc-base:gpu-1"
stock    = "YOUR-STOCK-IMAGE:TAG"
```

Rules of thumb:

- **A tag is mutable.** The box runs whatever the tag points to when it starts: preflight warns for every image that is not pinned by
  `@sha256:<digest>`, in the same words the controller uses for its own image setting. Pin by digest when you need reproducibility
  (`ghcr.io/OWNER/ddc-base@sha256:...`); at the least use a versioned tag (`...-1`, `...-2`), never `:latest`.
- Providers cache layers per node by reference, and the provider history that `ddc job` uses to prefer hosts that already pulled the image is
  keyed by the exact reference, so a moving tag also defeats the cache heuristic.
- Keep the layers stable: put rarely changing layers first so a rebuilt tag still reuses cached layers on providers.
- Docker Hub, GHCR and Quay all work; avoid registries that rate-limit anonymous pulls.
- Test the image once with `ddc job run examples/cpu-smoke.toml` (from a checkout of the repository) pointed at it before trusting it for a big run.
