# Stage 5 Model Selection

## Purpose

Stage 5 receives the fully assessed catalog and supports two server-installation
selection modes:

- automatic: rank eligible candidates and select the best candidate.
- manual: show the assessed catalog and let the operator choose one artifact.

Both modes preserve Stage 2 through Stage 4 results. Neither mode recalculates
backend compatibility, resources, fit status, or runtime parameters. This is an
operator-only installation/reconfiguration choice, not a per-user setting.

## Input

~~~text
selection_mode: automatic | manual
priority_preset: speed | balanced | quality
catalog_assessments: list[CatalogAssessment]
selected_artifact_id: string or null
risky_confirmed: boolean = false
~~~

priority_preset remains required in both modes because Stage 7 uses it to
resolve runtime parameters. selected_artifact_id is accepted only in manual
mode and must come from the displayed assessed catalog. Do not expose either
choice through per-user settings or model-selection environment variables.

## Candidate Sets

~~~text
automatic_candidates = assessments where auto_selectable == true
manual_candidates = assessments where manual_selectable == true
~~~

NOFIT, hidden, disabled, and blocked entries remain diagnostic rows but cannot
be selected. Automatic-disabled entries may still be manually selected when
manual_selectable is true, and vice versa.

## Automatic Mode

Automatic mode ranks FIT candidates first. RISKY candidates are considered only
when no FIT candidate exists.

~~~text
if fit_candidates:
    select rank 1 FIT
elif risky_candidates and risky_confirmed:
    select rank 1 RISKY
elif risky_candidates:
    return RISKY_CONFIRMATION_REQUIRED
else:
    return NO_SELECTABLE_MODEL
~~~

RISKY never outranks FIT. Ranking uses the existing deterministic keys:

~~~text
speed:    TPS, headroom, catalog priority, parameters, precision
balanced: catalog priority, headroom, parameters, precision, TPS
quality:  parameters, precision, max context, headroom, TPS, catalog priority
~~~

All numeric fields sort descending, null measurements sort last, and
artifact_id ascending is the final tie-break.

## Manual Mode

Manual mode displays every assessed catalog row, including diagnostic rows, but
accepts only an artifact where manual_selectable is true.

~~~text
if selected_artifact_id is null:
    return MANUAL_SELECTION_REQUIRED
if selected artifact is not manual_selectable:
    return INVALID_MANUAL_SELECTION
if selected artifact is RISKY and risky_confirmed == false:
    return RISKY_CONFIRMATION_REQUIRED
return SELECTED
~~~

Manual selection may choose a RISKY artifact even when FIT artifacts exist, but
only after explicit confirmation. It may never choose NOFIT.

## Output

~~~text
SelectionResult:
  selection_status:
    SELECTED
    | RISKY_CONFIRMATION_REQUIRED
    | MANUAL_SELECTION_REQUIRED
    | INVALID_MANUAL_SELECTION
    | NO_SELECTABLE_MODEL
  selection_mode: automatic | manual
  priority_preset: speed | balanced | quality
  selected_model: SelectedModel or null
  ranked_artifact_ids: list[string]
  reason_code: string

SelectedModel:
  artifact_id: string
  catalog_assessment: CatalogAssessment
  selection_mode: automatic | manual
  priority_preset: speed | balanced | quality
  risky_confirmed: boolean
  selection_reason_code: string
~~~

Reason codes include:

- SELECTED_FIT_BY_SPEED, SELECTED_FIT_BY_BALANCED, SELECTED_FIT_BY_QUALITY
- SELECTED_RISKY_BY_SPEED, SELECTED_RISKY_BY_BALANCED, SELECTED_RISKY_BY_QUALITY
- SELECTED_MANUAL_FIT, SELECTED_MANUAL_RISKY
- RISKY_CONFIRMATION_REQUIRED
- MANUAL_SELECTION_REQUIRED
- INVALID_MANUAL_SELECTION
- NO_SELECTABLE_MODEL

Stage 6 receives exactly one immutable selected CatalogAssessment snapshot.

## Prohibitions

Stage 5 must not select NOFIT or ineligible entries, probe hardware again,
download/start models for ranking, perform endpoint checks, use request-time
history, create per-user model preferences, or silently create a fallback model.
