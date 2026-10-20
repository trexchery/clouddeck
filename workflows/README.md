# Ready-made workflows (`ddc job`)

Each spec in this directory is a complete `ddc job` workflow: it picks a GPU, installs ComfyUI, downloads and verifies the models, runs the generation and
pulls the video back, then closes the lease. They are job specs like `examples/`, but real ones: models, versions and checksums are pinned.

| Spec | Family | You provide | GPU | Models |
|---|---|---|---|---|
| `wan22-ti2v-5b.toml` | ordinary generation | a prompt (optionally a start image) | 24 GB and up | 17 GiB |
| `wan22-t2v-a14b.toml` | ordinary generation | a prompt | 24 GB and up | 35 GiB |
| `wan22-i2v-a14b.toml` | ordinary generation | a start image and a prompt | 24 GB and up | 35 GiB |
| `wan22-animate.toml` | **character swap** | a reference image + a driving video | 80 GB class | 44 GiB |
| `scail2.toml` | **character animation / swap** | a reference image + a driving video | 80 GB class | 43 GiB |
| `scail2-fp8.toml` | same, lighter weights | a reference image + a driving video | 32 GB and up | 29 GiB |

Two different kinds of work, kept apart on purpose:

* **Ordinary generation** (`wan22-ti2v-5b`, `wan22-t2v-a14b`, `wan22-i2v-a14b`): a video made from a text prompt, or from a start image and a prompt. No driving video.
* **Character swap** (`wan22-animate`, `scail2`): the person in a **driving video** becomes the character of a **reference image**. They need both files.
  *Wan 2.2 Animate* replaces the person in the driving video's own scene (`replace`, background and camera kept) or moves the character into the reference image's scene
  (`move`). *SCAIL-2* animates the reference character with the driving motion (`animation`) or puts it in the driving video's place (`replacement`, with a relight LoRA).
  In both, SAM3 finds the people by text, so there are no masks, click points or skeletons to prepare.

## Run one

```bash
cp my-character.png workflows/inputs/reference.png      # character-swap workflows; a start.png for wan22-i2v-a14b; nothing for the text-only ones
cp my-motion.mp4    workflows/inputs/driving.mp4
ddc job check workflows/scail2.toml                      # read-only: prices, availability, the budget math, your inputs
ddc job run   workflows/scail2.toml --approve-usd 10     # rents the cheapest matching GPU, runs, retrieves, verifies the close
```

The video and a small `.json` (every setting, the seed and the exact graph, so a result can be reproduced) land in `workflows/outputs/`. The specs ask for up to $3.20/h
for the 80 GB class; the default ceiling in `config.toml` is $2/h (`max_price_usd_hr`), and `ddc job check` says when a spec is lowered to it. Read
[docs/COSTS-AND-SAFETY.md](../docs/COSTS-AND-SAFETY.md#jobs-ddc-job) first: a job lease is real money.

Settings are environment variables in each spec's `[env]` table (commented out with their defaults; each spec lists its own). Sizes must be multiples of 16 (Wan 2.2 A14B,
Animate) or 32 (the 5B model, SCAIL-2) and frame counts `4k+1`; a wrong value fails in the first second, before any download is used.

## Make your own

Your own ComfyUI graph runs the same way, without writing any code:

1. In ComfyUI, build and test the workflow, then **Save (API Format)** (settings > enable dev mode options) to `workflows/inputs/graph.json`; put the images and videos it loads in `workflows/inputs/` too.
2. Copy `templates/custom-comfyui.toml` to `my-<name>.toml` in this directory, set `name`, and add one `[[cache.item]]` per model file the graph loads (the template shows the shape; use a URL pinned to a
   commit and the sha256 from the model page). List the media under `inputs` and add each `cache-<name>` to the `generate` step's `needs`.
3. `ddc job check workflows/my-<name>.toml`, then `ddc job run ... --approve-usd N`. Images, GIFs and videos the graph saves come back in `outputs/`.

Only ComfyUI's own nodes are installed (`COMFY_NODES` loads node folders you added to the prebuilt image). The spec and graph are plain files: commit them to keep the workflow. With the `clouddeck`
skill, Claude can do steps 1 and 2 for you. The custom mode was run on a real GPU with the graph of the 5B workflow.

## Prompting

Write the prompt as plain sentences, in `[env]` (`WAN_PROMPT`, `ANIMATE_PROMPT`, `SCAIL_PROMPT`) or, for anything longer than a line, as `prompt.txt` listed under `inputs`
(`inputs = [..., "inputs/prompt.txt"]`). The environment variable wins when both are set. The `[env]` values are part of the lease request and visible to the provider; a
`prompt.txt` travels over SSH only.

* **Wan 2.2 (ordinary generation).** One or two descriptive sentences work, a paragraph works better: the subject, what it does, the place, the camera (slow push-in,
  handheld, low angle), the light (golden hour, soft window light) and the look. For image-to-video describe the motion and the camera, not what is already in the image.
  The negative prompt of the official templates is built in (override with `WAN_NEGATIVE`).
* **Wan 2.2 Animate (character swap).** Describe the *new* character and the scene in natural language ("A woman in a red leather jacket dances in a sunlit kitchen"). In `replace`
  mode the background and camera come from the driving video, so keep the scene part short and consistent with it.
* **SCAIL-2.** The authors trained it on long, detailed captions, so write one: hair, clothing, build, the setting, the action and the light, in full sentences. A one-liner
  works but is less faithful.

## Which GPU

`[hardware.gpu].models` lists every card the workflow accepts and `min_vram_gi` the floor; `ddc job` takes the cheapest bid among providers that have one, preferring a provider that
already pulled the image. Nothing is vendor-locked beyond that list. The 80 GB specs list the cards the character-swap pipelines were run on (A100 and H100 80 GB; the
96 GB workstation and server cards are in the list too). The 24 GB specs rely on ComfyUI's offloading for the 14B models and were measured only on a 16 GB card. Blackwell
Ultra (B300) is not listed: it needs a CUDA 13 torch.

ComfyUI sizes its offloading from the VRAM it finds. The runner adds only `--highvram` at 60 GiB and more (models stay resident) and `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`; it
refuses to start on a GPU the image's torch has no kernels for, and prints the GPU it found. Extra ComfyUI flags: `COMFY_EXTRA_ARGS`.

## Less setup: prebuilt environment, cached weights

Setup is the part you pay for without getting a video, so there are two ways to remove it. Both are optional and the specs work without them.

1. **Prebuilt image.** `comfyui/Dockerfile` bakes what the install step does otherwise (ComfyUI at a pinned commit, its requirements, the two Animate custom nodes at pinned commits, the
   DWPose weights, sshd and the system packages) on the same PyTorch base the stock path uses, pinned by digest. `install.sh` is the one script for both: on the prebuilt image it
   finds everything and returns in seconds (18 s measured in a local container, including a `pip` consistency check), on the stock image it installs while the models download.
   ```bash
   docker build -f workflows/comfyui/Dockerfile -t ghcr.io/OWNER/clouddeck-comfyui:0.42.1-1 workflows/comfyui
   docker push ghcr.io/OWNER/clouddeck-comfyui:0.42.1-1        # make the package public: providers pull anonymously (docs/prebuilt-image.md)
   ```
   then uncomment `prebuilt = "ghcr.io/OWNER/clouddeck-comfyui:0.42.1-1"` in the specs (better: `...@sha256:<digest>`). If it cannot be pulled, `ddc job` falls back to the stock image
   by itself. Models are deliberately not in the image (tens of GB): they are what the next point is for.
2. **Cached weights.** Every model is a `[[cache.item]]`: downloaded in parallel the moment the box is up, resumable, sha256-verified and pinned to an immutable Hugging Face
   commit. To stop depending on Hugging Face's speed, put the files in any HTTP(S) store under their `path` names and list it once: `mirrors = ["https://your-store/models"]`
   under `[cache]`; mirrors are tried first, Hugging Face stays the fallback, a wrong byte is caught by the checksum. A provider that already ran the image keeps its layers, which is
   why `ddc job` prefers it.

Other things that shorten a run, already in the specs: the install step runs while the models download; the lease is timed so setup ends when the inputs are ready; `idle_abort_s`
closes a box that is doing nothing; `max_cost_usd` and `runtime_hours` bound the worst case.

## What runs on the box

`comfyui/comfy_run.py` builds the ComfyUI API prompt for the chosen workflow (the official templates from Comfy-Org/workflow_templates, node for node), starts ComfyUI on
127.0.0.1 (only SSH is exposed), queues the prompt, waits, and saves the video to `/work/out`. Long character-swap clips are chained inside one prompt: Animate in windows of
77 frames (72 new each, continuing from the last 5), SCAIL-2 in segments of 81 frames (76 new each, colour-matched to the previous tail). The default is the whole driving clip up to
300 frames. For Animate the runner first cuts 512 px face crops of the driver with a YuNet face detector and feeds them instead of the template's face dots (this keeps the new
character's expressions; `ANIMATE_FACE = "dots"` turns it off). Those crops are encoded with NVENC when the box exposes it, libx264 on the CPU otherwise (the log says which);
the final video is encoded by ComfyUI's `SaveVideo` (H.264, CRF 18, CPU).

## What was and was not verified

Every spec was executed end to end on a real GPU, in a local GPU container that is driven exactly like a rented box (inputs uploaded to `/work/in`, every cache item and step
launched as the job runner launches them, the stock image so the install step really installed). The GPU was an RTX 5080 (16 GB), so the runs were small and short on purpose:
`WAN_FAST=1` (the 4-step lightx2v LoRAs), 512 px, 33 frames for the Wan 2.2 specs, short clips for the character-swap ones. The 14B models ran fp8 with ComfyUI's offloading.

| Spec | Result | Wall time (downloads included) |
|---|---|---|
| `wan22-ti2v-5b` | 33-frame H.264 video | 383 s |
| `wan22-t2v-a14b` | video, models cold | 718 s, generation 38 s |
| `wan22-i2v-a14b` | 33 frames, 512x512, from a start image, models already downloaded | 154 s, generation 47 s |
| `scail2-fp8` | animation of a reference character from a driving clip | 648 s, generation 143 s |
| `wan22-animate` | `replace` on a driving clip, fp8 Animate weights (`ANIMATE_UNET`) | 711 s, generation 250 s |

* The stock install (no prebuilt image) took 305 s in the first run and overlaps the downloads; with the install already done (prebuilt image, or a second run) the step takes 16 to 20 s.
* The graph of every workflow and option set is also accepted by a real ComfyUI v0.42.1 server (all node classes present, every input validated) with the pinned custom nodes.
  Pins: ComfyUI `v0.42.1` (`63be2f9`), controlnet_aux `a587a4c`, KJNodes `17195fc`.
* The prebuilt image builds, keeps the CUDA torch, and the install step is idempotent.
* NVENC: the face-crop step encodes with `h264_nvenc` when the container exposes the video driver capability and falls back to libx264 otherwise (the log says which).
  `SaveVideo`'s final encode is on the CPU either way.
* **Not verified:** nothing here was run on a rented Akash box by this repository, and not at full size. The bf16 80 GB `wan22-animate` and `scail2` weights, 720p and the long
  chained clips (several windows or segments) were not run on a GPU of that class; those parts come from earlier runs on A100 and H100 80 GB boxes, and the specs reuse those numbers.
  Time and cost figures in the specs are ESTIMATED. The 24 GB specs rely on offloading and were measured only on the 16 GB card above.
* Use only media you have the right to use, and read each model's license on its model card before commercial use.
