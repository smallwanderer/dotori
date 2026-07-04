# Stage 4 Fit and Eligibility Evaluation

## Purpose

Stage 4 compares the fixed Stage 3 baseline placement with currently available
RAM, per-device VRAM, and disk. It assigns exactly one resource `fit_status` and
separately evaluates catalog eligibility.

Stage 4 must not select a backend, change device placement, reduce context,
change concurrency, move KV cache, or alter GPU layers.

## Inputs

```text
HardwareProfile
RAGModelCatalogEntry
BackendResolution
ResourceEstimation or null when the backend is unavailable
```

Stage 4 uses the `HardwareProfile` field names directly:

- `effective_ram_available_mb`
- `gpu_probe_result.devices[i].free_vram_mb`
- `disk_free_mb`

`disk_required_mb` always comes from `ResourceEstimation`, not directly from
the raw artifact catalog.

## Output

```text
FitEvaluation:
  fit_status: FIT | RISKY | NOFIT
  fit_reason_codes: list[string]
  source_reason_code: string or null
  summary: string
  ram_check: PoolCheck or null
  vram_checks: list[VramPoolCheck]
  disk_check: PoolCheck or null

PoolCheck:
  required_mb: integer
  available_mb: integer
  hard_fit: boolean
  soft_fit: boolean

VramPoolCheck:
  device_index: integer
  required_mb: integer
  available_mb: integer
  hard_fit: boolean
  soft_fit: boolean

CatalogEligibility:
  catalog_status: ACTIVE | DISABLED | BLOCKED
  visible: boolean
  auto_select_allowed: boolean
  manual_select_allowed: boolean
  reason_code: string
  auto_selection_reason_codes: list[string]
  manual_selection_reason_codes: list[string]
  auto_selectable: boolean
  manual_selectable: boolean
```

`fit_reason_codes` is empty only for `FIT`. `summary` is human-readable;
program logic uses the codes and booleans.

## Short-Circuit Rules

Apply these before pool checks:

1. `backend_status == UNAVAILABLE` produces `NOFIT` with the Stage 2
   `reason_code`. All pool checks are null or empty.
2. `estimation_status == FAILED` produces `NOFIT` with the Stage 3
   `failure_code`. All pool checks are null or empty.
3. Otherwise evaluate the exact Stage 3 placement without modifying it.

Null TPS does not affect fit status.

## Pool Thresholds

Memory uses:

```text
memory_headroom_multiplier = 1.25

hard_fit = required_ram_mb <= effective_ram_available_mb
soft_fit =
    required_ram_mb * memory_headroom_multiplier
    <= effective_ram_available_mb
```

The same memory formula applies per GPU by replacing `required_ram_mb` and
`effective_ram_available_mb` with `required_vram_per_gpu_mb[i]` and
`gpu_probe_result.devices[i].free_vram_mb`.

Disk uses:

```text
disk_headroom_multiplier = 1.15

hard_fit = disk_required_mb <= disk_free_mb
soft_fit =
    disk_required_mb * disk_headroom_multiplier <= disk_free_mb
```

Do not add RAM and VRAM into one pool. Evaluate `required_ram_mb` once and each
entry of `required_vram_per_gpu_mb` against the device at the same list
position. Zero requirements for unused devices pass both checks.

## Status Resolution

Evaluate every applicable RAM, VRAM, and disk check, then resolve once:

```text
if any check has hard_fit == false:
    fit_status = NOFIT
elif any check has soft_fit == false:
    fit_status = RISKY
else:
    fit_status = FIT
```

Reason codes are accumulated in deterministic order: backend/estimation, RAM,
VRAM by device position, then disk.

Hard-failure codes:

- `BACKEND_UNAVAILABLE`
- `ESTIMATION_FAILED`
- `RAM_INSUFFICIENT`
- `VRAM_INSUFFICIENT:<device_index>`
- `DISK_INSUFFICIENT`

Headroom-risk codes:

- `RAM_HEADROOM_LOW`
- `VRAM_HEADROOM_LOW:<device_index>`
- `DISK_HEADROOM_LOW`

For a short circuit, `source_reason_code` retains the exact Stage 2
`reason_code` or Stage 3 `failure_code`. It is null for normal pool evaluation.

## Catalog Eligibility

Resource fit and catalog policy are independent dimensions.

Resolve `catalog_status` in this order:

```text
if policy.safety == danger:
    catalog_status = BLOCKED
    reason_code = SAFETY_BLOCKED
elif enabled == false:
    catalog_status = DISABLED
    reason_code = CATALOG_DISABLED
else:
    catalog_status = ACTIVE
    reason_code = CATALOG_ACTIVE
```

Presentation and selection fields are derived separately:

```text
visible = policy.visible
auto_select_allowed = policy.allow_auto_select
manual_select_allowed = policy.allow_manual_select

auto_selectable = (
    catalog_status == ACTIVE
    and visible
    and auto_select_allowed
    and fit_status in {FIT, RISKY}
)

manual_selectable = (
    catalog_status == ACTIVE
    and visible
    and manual_select_allowed
    and fit_status in {FIT, RISKY}
)
```

The corresponding selection reason list contains every applicable reason:

- `CATALOG_NOT_ACTIVE`
- `HIDDEN_BY_POLICY`
- `AUTO_SELECTION_DISABLED`
- `MANUAL_SELECTION_DISABLED`
- `RESOURCE_NOFIT`

Each list is empty when its corresponding selectable field is true.

An ineligible entry may still have a valid resource `fit_status`. Do not encode
`DISABLED` or `DANGER` as fit statuses.

## Stage 5 Boundary

Stage 4 produces assessment data only. Sorting, hiding unavailable entries,
applying the requested `speed`/`balanced`/`quality` preference, warnings, and
automatic ranking and optional operator catalog selection belong to Stage 5.
