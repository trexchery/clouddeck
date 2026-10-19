# Choosing any CPU or GPU configuration

CloudDeck is not limited to presets. The **hardware picker** lets you rent whatever the network advertises right now: any CPU shape, any
GPU vendor, model, VRAM variant and interface, in any count, in a region you choose, from one provider you pick or from any. Presets
(`16`, `32`, `64`, your own `[profiles.*]`) stay as one-key shortcuts in the normal deploy dialog.

Open it with `h` on the Compute tab, with **Hardware...** (or `F2`) inside the deploy dialog, from the command palette
(*Browse hardware...*), or with `Enter` on a row of the Providers tab (the picker then starts pinned to that provider).
`clouddeck -p demo` shows the whole feature offline with a simulated market (several GPU vendors, VRAM variants, a model nobody has
heard of, an ARM provider).

The picker never deploys anything. It returns an order; the deploy dialog, the spend-guard review and the confirmation that follow are the
ones you already know, and the controller checks every limit again at deploy time.

## What the lists are made of

Nothing is hardcoded: no GPU model, vendor, architecture or region appears in the code. The lists are built from four public, unauthenticated
Akash Console API reads, each cached and each allowed to fail on its own:

| Source | Used for | Cache |
|---|---|---|
| `GET /v1/providers` | every online provider: free CPU / RAM / disk / GPUs (provider-wide totals), uptime, audited flag, the CPU architecture / vendor / region it **states**, the GPU kinds it has | 5 min (one download shared by the provider table and the catalogue) |
| `GET /v1/gpu-models` | every GPU kind the network supports, including ones nobody offers right now | 24 h |
| `GET /v1/placement-options` | free units per VRAM / interface variant and the **most GPUs one node has free** (audited providers) | 2 min |
| `GET /v1/gpu-prices` | market price per GPU-hour per variant (min / average / max from past leases) | 10 min |

So a new vendor (AMD is already in the supported list), a new model, or a new VRAM variant shows up in the dropdowns as soon as the network
lists it. If you know a model before any list does, choose **Other (type it)** and type its id; the order is allowed and flagged
*not currently advertised*, and availability screening will tell you whether anyone can take it.

Anything a provider does not state is shown as unknown and **never guessed**. A provider that does not state its CPU architecture is left
out when you choose an architecture (an order that requires the attribute could not be bid on by it anyway), and the form says how many
providers that affects. CPU **models** (for example "EPYC 7713") are shown where known but cannot be selected: almost no provider states them
reliably.

## Advertised, screened, confirmed

Three different levels of evidence are kept apart everywhere (table, compare view, summary, confirmation):

| State | What it means | Price |
|---|---|---|
| **advertised** | A provider reports this inventory. Provider-wide totals, so an upper bound: 40 free vCPUs in total does not mean one 32 vCPU machine exists. | an **estimate**, with its source named |
| **screened** | You pressed *Check availability* and the network's bid-screening says a node of this provider fits **this exact order** (capacity and attributes). | still **no price**, and not a bid |
| **confirmed** | A real bid with a price. These only exist after an order has been placed through the normal deploy flow. | the bid; shown in the instance detail |

Nothing in the picker can create a deployment to find out a price: confirmed prices appear only after you have confirmed an order, whose
price cap and runtime limit were shown beforehand. The picker's own network use is limited to the public reads above, the public pricing
reference, and the explicit availability check.

### Estimates and their sources

An estimate is CPU/RAM/disk plus GPUs. The source is shown in the table (*Source* column) and in the summary:

- **own history**: the provider's price per vCPU-hour observed in your own past leases there (provider-specific; the GPU part is the market
  average);
- **market avg**: the network-wide reference price (`/v1/pricing`) plus the market average per GPU-hour of the matching variant. These are the
  same for every provider until bids exist, so the table does not pretend to rank providers by price;
- **-**: unknown (for example a GPU model with no recent leases). Never filled in with a guess.

### Check availability

`Check availability` (button, or `a` with the table focused) sends the order to the network's bid-screening endpoint. It is an explicit
action only: editing the form never calls it. Answers are cached for 60 seconds per distinct order, requests are rate limited (about one per
two seconds), and the table is marked *order changed since the last check* as soon as you edit the form. Providers the network did not return stay
*advertised*, with a note. If screening is unreachable, the rows stay advertised and the header says so.

Screening can disagree with the advertised lists: for example a VRAM + interface combination that exists on price lists may match no
provider's attributes. Treat *screened* as the stronger statement.

## The picker

- **CPU only / GPU** (arrow keys, then Space). In GPU mode: vendor, model (free units first, with providers and the biggest order one node can
  hold), *also accept these models* (an any-of list of ids), a VRAM / interface variant with free units and market price, and the GPU count. A
  count larger than any single node has free is a warning, not an error.
- **CPU architecture** and **CPU vendor** (shown only when providers state them), **region**, **vCPU / RAM / disk**, **audited providers only**
  (the order requires the network auditor's signature) and **only providers with room now**.
- The offers table: provider, region, audited, uptime, incidents in 7 days (after a check), free capacity, state, estimate and its source. `/`
  searches, `s` cycles the sort column, a header click sorts by it.
- **Space** marks up to three offers, `c` compares them side by side, **Enter** (or *Pin* in the compare view) chooses a provider: only bids from
  it will be accepted, and its region is pre-filled. `u` removes the pin.
- Below the table, always visible before you can continue: the **cost ceiling** (price cap x runtime hours), the estimate with its source, the
  **spend-guard verdict** (the same central check that runs at deploy time, shown before you commit), and the advertised-vs-confirmed note.
- Empty results explain themselves: *No provider currently advertises capacity for ... Closest available: 1 provider advertises capacity for
  8 GPUs instead of 100 (at most 8 per node) and any region*. Offline: the last saved hardware list is shown with its age
  (`hw-catalog.json` next to your state); with nothing saved, vendor and model can be typed by hand, with a notice that nothing was checked
  against live inventory.

Everything is reachable by keyboard (Tab / Shift+Tab, arrows, Enter, Space, Esc); state is always written out in words, colour only adds to it.

## Provider pinning and audited providers

Akash picks the provider by auction; CloudDeck can only narrow who may win, never widen:

- **Pin**: the order is created as usual, and only bids from the chosen provider(s) are accepted. If the provider does not bid (or its
  bid is above your cap, or it fails the uptime / lease-count / bad-provider rules, which a pin never bypasses) the result is the normal
  *no acceptable bids* outcome: the deployment this call created is closed again through the same ownership-verified path, no lease is made, and
  the task window shows it as an ordinary failed attempt.
- **Audited providers only**: the order carries the network auditor's address in `signedBy` (taken from the network data: the auditor that
  signs the most providers), and bids from providers that are not audited are rejected as well. If that address cannot be determined (no
  network and nothing saved) the order is refused instead of silently going to everyone.

## Limits and safety

Custom hardware changes **nothing** about spending protection:

- Every order carries a price cap in the SDL (`pricing.amount`, uact per block, rounded **down** so it never exceeds your cap, and at least 1)
  and a platform-enforced runtime limit (`runtimeLimitHours`, 1 to 48, the Console API's range; the controller's own hard ceiling is 24 h unless
  `unsafe_disable_limits` is set).
- The budget guard, reserve, `max_total_leases`, `max_workers`, hard runtime ceiling and per-call ceilings apply exactly as before, with the budget
  guard on or off.
- **Price ceiling of one profile**: `max_price_usd_hr` in `config.toml` (default `2.0`, at most `100`). GPUs cost more than the default allows, so
  you raise this yourself, explicitly:

  ```toml
  max_price_usd_hr = 10.0      # price caps above this are refused (default 2.0, absolute maximum 100)
  ```

  A malformed value is reported by the config check and never widens the limit. `unsafe_disable_limits = true` still lifts the ceiling, with its
  usual loud warning, exactly as before.
- **Size limits** (fat-finger bounds, optional; defaults are what earlier versions allowed, the maxima are the network's own):
  `max_cpu` (128, up to 384), `max_mem_gb` (1024, up to 2048), `max_disk_gb` (4000, up to 32768), `max_gpu` (8, up to 24).
- Generic instances (`box`) have no idle shutdown; their only brake is cap x hours, so the dialogs show that worst case before you confirm.
- The remote API still offers presets only; custom hardware is a local, interactive feature.

## What is written to the SDL

Typed text never reaches the SDL as text: tokens (vendor, model, interface, architecture, region) must be 1-64 characters of `a-z 0-9 . _ -`
(case is folded; "RTX 4090" becomes `rtx4090`), VRAM must look like `24Gi`, and the SDL is built from fixed line templates and checked before it is
sent. Orders map to:

- GPU: `resources.gpu.attributes.vendor.<vendor>` with one `- model:` entry per accepted model (any-of), each with `ram:` / `interface:` when
  chosen. A vendor without a model means any model of that vendor. VRAM or interface without a model is refused.
- CPU architecture / vendor / region: `placement.akash.attributes` (`capabilities/cpu/arch`, `capabilities/cpu`, `location-region`).
- Audited only: `placement.akash.signedBy.anyOf`.
- Pins are not SDL: they filter bids on your PC.

Profiles without the new keys render byte for byte what earlier versions did. Orders made in the picker are saved as custom profiles
(`custom_profiles.json`, name `x<cpu>-<ram>-<disk>[-g<n><model>]` plus a short hash when placement is set); new keys are written only when
used, so older files and older readers are unaffected.

The default container image is Debian based and x86 oriented. ARM or AMD GPU orders may need an image that supports them: set `image` in
`config.toml` (pin it by digest).

## Troubleshooting

| You see | Meaning / what to do |
|---|---|
| *Offline: hardware list from 3.0 h ago* | The network could not be reached; the saved list is shown. Retry later or work with it knowingly. |
| *Offline: no hardware list could be loaded* | Nothing saved yet and no network. Type vendor / model by hand; nothing is checked until bids arrive. |
| *No provider currently advertises capacity for ...* | Read the *Closest available* line; fewer GPUs, any VRAM / interface, another region, unaudited, or switch off *only providers with room now*. |
| *screening unavailable ... advertised capacity only* | The availability check could not be completed (network or rate limit); the rows are still the advertised ones. |
| Order blocked: *above max_usd_hr_hard* | Your price cap is above the ceiling; lower the cap, or raise `max_price_usd_hr` in `config.toml` on purpose. |
| *no acceptable bids (only bids from the chosen provider(s) are accepted)* | The pinned provider did not bid within your cap. The created deployment was closed; unpin or raise the cap. |
| *cannot prepare an audited-only order* | The auditor address is unknown offline; retry with network access. |

Data files: `hw-catalog.json` (the last complete hardware list, safe to delete) and `custom_profiles.json` (orders made in the picker).
