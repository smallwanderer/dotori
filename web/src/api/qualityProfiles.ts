import { apiRequest } from './http'

export type QualityAxis = 'retrieval' | 'generation' | 'prompt_policy'
export type ProfileStatus = 'draft' | 'active' | 'archived'
export type ValidationState = 'verified' | 'unverified' | 'stale' | 'rollback' | 'not_run'

export interface QualityProfileRevision<T extends Record<string, unknown>> {
  uid: string
  version: number
  revision: number
  status: ProfileStatus
  change_axis: QualityAxis | null
  based_on_uid: string | null
  overrides: Partial<T>
  effective: T
  changed_fields: string[]
  validation: {
    state: ValidationState
    last_run_uid: string | null
    warnings: string[]
  }
  note: string
  updated_at: string
  applied_at: string | null
}

export interface ProfilePermissions {
  can_read: boolean
  can_edit: boolean
  can_apply: boolean
}

export interface DraftConflict {
  change_axis: QualityAxis
  uid: string
}

export interface QualityProfileEnvelope<T extends Record<string, unknown>> {
  ok: true
  workspace_uid: string
  axis: QualityAxis
  active: QualityProfileRevision<T>
  draft: QualityProfileRevision<T> | null
  defaults: T
  schema: Record<string, QualityFieldSchema>
  capabilities: Record<string, unknown>
  permissions: ProfilePermissions
  draft_conflict?: DraftConflict | null
}

export interface QualityFieldSchema {
  type: 'number' | 'integer' | 'boolean' | 'enum' | 'string'
  tier: 'core' | 'advanced'
  minimum?: number
  maximum?: number
  step?: number
  choices?: string[]
  effects?: string[]
  recommended_minimum?: number
  recommended_maximum?: number
}

export interface RetrievalConfig extends Record<string, unknown> {
  dense_weight: number
  sparse_weight: number
  search_top_k: number
  rag_search_top_k: number
  retrieval_threshold: number | null
  evidence_top_k: number
  evidence_context_window: number
  candidate_multiplier: number
  per_node_candidate_cap: number
  query_sparse_top_n: number
  pooling_method: 'normalized_logsumexp' | 'normalized_softmax' | 'max'
  pool_top_k: number
  pool_tau: number
  doc_length_penalty_alpha: number
  contextual_compression: { enabled: boolean }
}

export interface GenerationConfig extends Record<string, unknown> {
  max_output_tokens: number
  temperature: number
  top_p: number
}

export type PromptRoute = 'document_rag' | 'no_retrieval'
export interface PromptRoutePolicy {
  mode: 'inherit' | 'replace'
  instruction: string | null
  sha256?: string
  character_count?: number
  server_prompt_contract_version?: number
}

export interface PromptPolicy extends Record<string, unknown> {
  document_rag: PromptRoutePolicy
  no_retrieval: PromptRoutePolicy
}

export interface PromptProfileEnvelope extends QualityProfileEnvelope<PromptPolicy> {
  fixed_contract?: string
  provider_disclosure?: string | null
}

export interface PromptPreview {
  ok: true
  route: PromptRoute
  assembled_prompt: string
  sha256: string
  character_count: number
  server_prompt_contract_version: number
}

interface DraftMutation<T extends Record<string, unknown>> {
  ok: true
  draft: QualityProfileRevision<T> | null
}

const BASE = '/api/workspaces/v1/current'

function profileApi<T extends Record<string, unknown>>(path: string) {
  return {
    get: () => apiRequest<QualityProfileEnvelope<T>>(`${BASE}/${path}/`),
    saveDraft: (expectedRevision: number, overrides: Partial<T>, resetFields: string[], note: string) => apiRequest<DraftMutation<T>>(`${BASE}/${path}/draft/`, {
      method: 'PATCH',
      json: { expected_revision: expectedRevision, overrides, reset_fields: resetFields, note },
    }),
    discardDraft: (expectedRevision: number) => apiRequest<DraftMutation<T>>(`${BASE}/${path}/draft/discard/`, {
      method: 'POST',
      json: { expected_revision: expectedRevision },
    }),
    apply: (expectedRevision: number, evaluationRunUid: string | null, allowUnverified: boolean, note: string) => apiRequest<QualityProfileEnvelope<T>>(`${BASE}/${path}/apply/`, {
      method: 'POST',
      json: {
        expected_revision: expectedRevision,
        evaluation_run_uid: evaluationRunUid,
        allow_unverified: allowUnverified,
        note,
      },
    }),
  }
}

export const retrievalProfileApi = profileApi<RetrievalConfig>('retrieval-profile')
export const generationProfileApi = profileApi<GenerationConfig>('generation-profile')

export const promptProfileApi = {
  get: () => apiRequest<PromptProfileEnvelope>(`${BASE}/system-prompt/`),
  saveDraft: (expectedRevision: number, overrides: Partial<PromptPolicy>, note: string) => apiRequest<DraftMutation<PromptPolicy>>(`${BASE}/system-prompt/draft/`, {
    method: 'PATCH',
    json: { expected_revision: expectedRevision, overrides, note },
  }),
  discardDraft: (expectedRevision: number) => apiRequest<DraftMutation<PromptPolicy>>(`${BASE}/system-prompt/draft/discard/`, {
    method: 'POST',
    json: { expected_revision: expectedRevision },
  }),
  preview: (expectedRevision: number, route: PromptRoute) => apiRequest<PromptPreview>(`${BASE}/system-prompt/draft/preview/`, {
    method: 'POST',
    json: { expected_revision: expectedRevision, route },
  }),
  apply: (expectedRevision: number, evaluationRunUid: string | null, allowUnverified: boolean, note: string) => apiRequest<PromptProfileEnvelope>(`${BASE}/system-prompt/apply/`, {
    method: 'POST',
    json: {
      expected_revision: expectedRevision,
      evaluation_run_uid: evaluationRunUid,
      allow_unverified: allowUnverified,
      note,
    },
  }),
}

export interface QualityProfileVersionSummary {
  uid: string
  version: number
  change_axis: QualityAxis
  validation: { state: ValidationState }
  note: string
  applied_at: string | null
  created_by: { id: number; display_name: string } | null
}

export const qualityProfileApi = {
  listVersions: (axis?: QualityAxis) => {
    const query = axis ? `?axis=${axis}&page=1&limit=20` : '?page=1&limit=20'
    return apiRequest<{ ok: true; results: QualityProfileVersionSummary[] }>(`${BASE}/quality-profile/versions/${query}`)
  },
}

export interface EvaluationDatasetItem {
  query: string
  expected_node_ids: string[]
}

export interface EvaluationDataset {
  uid: string
  axis: QualityAxis
  name: string
  item_count: number
  created_at: string
}

export type EvaluationRunStatus = 'pending' | 'running' | 'succeeded' | 'failed'

export interface RetrievalEvaluationMetrics {
  queries: number
  chunk_count: number
  top_k: number
  dense_weight: number
  sparse_weight: number
  hit_rate_at_1: number
  hit_rate_at_k: number
  mrr_at_k: number
  per_query: { query: string; hit_at_1: boolean; matched_rank: number | null }[]
}

export interface EvaluationRun {
  uid: string
  axis: QualityAxis
  dataset_uid: string
  status: EvaluationRunStatus
  metrics: Partial<RetrievalEvaluationMetrics>
  error_message: string
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export const evaluationDatasetApi = {
  list: (axis: QualityAxis) => apiRequest<{ ok: true; axis: QualityAxis; datasets: EvaluationDataset[] }>(`${BASE}/evaluation-datasets/?axis=${axis}`),
  create: (axis: QualityAxis, name: string, items: EvaluationDatasetItem[]) => apiRequest<{ ok: true; dataset: EvaluationDataset }>(`${BASE}/evaluation-datasets/`, {
    method: 'POST',
    json: { axis, name, items },
  }),
}

export const evaluationRunApi = {
  startRetrieval: (expectedRevision: number, datasetUid: string) => apiRequest<{ ok: true; run: EvaluationRun }>(`${BASE}/retrieval-profile/evaluate/`, {
    method: 'POST',
    json: { expected_revision: expectedRevision, dataset_uid: datasetUid },
  }),
  get: (runUid: string) => apiRequest<{ ok: true; run: EvaluationRun }>(`${BASE}/evaluation-runs/${runUid}/`),
}
