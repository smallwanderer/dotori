import { type ChangeEvent, type FormEvent, useEffect, useMemo, useState } from 'react'
import type { ServerPolicySummary } from '../../api/models'
import {
  evaluationDatasetApi, evaluationRunApi,
  generationProfileApi, promptProfileApi, qualityProfileApi, retrievalProfileApi,
  type EvaluationDataset, type EvaluationRun, type GenerationConfig,
  type PromptPolicy, type PromptProfileEnvelope, type PromptRoute,
  type QualityAxis, type QualityFieldSchema, type QualityProfileEnvelope,
  type QualityProfileRevision, type QualityProfileVersionSummary, type RetrievalConfig,
} from '../../api/qualityProfiles'
import { tenantApi, type WorkspaceMember, type WorkspaceSummary } from '../../api/tenants'
import { ApiClientError } from '../../api/http'
import { Icon } from '../../components/Icon'
import { type TranslationKey, useI18n } from '../../i18n'

interface WorkspaceSettingsFeatureProps {
  policy: ServerPolicySummary | null
  loading: boolean
  failed: boolean
}

type SettingsTab = QualityAxis | 'evaluation' | 'general'
type EditableConfig = RetrievalConfig | GenerationConfig

const RETRIEVAL_FIELDS: (keyof RetrievalConfig)[] = [
  'dense_weight', 'sparse_weight', 'search_top_k', 'rag_search_top_k',
  'retrieval_threshold', 'evidence_top_k', 'evidence_context_window',
  'candidate_multiplier', 'per_node_candidate_cap', 'query_sparse_top_n',
  'pooling_method', 'pool_top_k', 'pool_tau', 'doc_length_penalty_alpha',
  'contextual_compression',
]
const GENERATION_FIELDS: (keyof GenerationConfig)[] = ['max_output_tokens', 'temperature', 'top_p']

const FALLBACK_SCHEMA: Record<string, QualityFieldSchema> = {
  dense_weight: { type: 'number', tier: 'core', minimum: 0, maximum: 1, step: 0.05, effects: ['retrieval_quality'] },
  sparse_weight: { type: 'number', tier: 'core', minimum: 0, maximum: 1, step: 0.05, effects: ['retrieval_quality'] },
  search_top_k: { type: 'integer', tier: 'core', minimum: 1, maximum: 50, step: 1, effects: ['retrieval_quality', 'latency'] },
  rag_search_top_k: { type: 'integer', tier: 'core', minimum: 1, maximum: 10, step: 1, effects: ['context_quality', 'latency'] },
  retrieval_threshold: { type: 'number', tier: 'core', minimum: 0, maximum: 1, step: 0.05, effects: ['retrieval_quality'] },
  evidence_top_k: { type: 'integer', tier: 'core', minimum: 1, maximum: 10, step: 1, effects: ['context_quality'] },
  evidence_context_window: { type: 'integer', tier: 'core', minimum: 0, maximum: 3, step: 1, effects: ['context_quality', 'latency'] },
  candidate_multiplier: { type: 'integer', tier: 'advanced', minimum: 1, maximum: 50, step: 1, effects: ['retrieval_quality', 'latency'] },
  per_node_candidate_cap: { type: 'integer', tier: 'advanced', minimum: 1, maximum: 20, step: 1, effects: ['retrieval_quality'] },
  query_sparse_top_n: { type: 'integer', tier: 'advanced', minimum: 1, maximum: 256, step: 1, effects: ['retrieval_quality', 'latency'] },
  pooling_method: { type: 'enum', tier: 'advanced', choices: ['normalized_logsumexp', 'normalized_softmax', 'max'], effects: ['retrieval_quality'] },
  pool_top_k: { type: 'integer', tier: 'advanced', minimum: 1, maximum: 20, step: 1, effects: ['retrieval_quality'] },
  pool_tau: { type: 'number', tier: 'advanced', minimum: 0.1, maximum: 20, step: 0.1, effects: ['retrieval_quality'] },
  doc_length_penalty_alpha: { type: 'number', tier: 'advanced', minimum: 0, maximum: 1, step: 0.05, effects: ['retrieval_quality'] },
  contextual_compression: { type: 'boolean', tier: 'advanced', effects: ['context_quality', 'latency'] },
  max_output_tokens: { type: 'integer', tier: 'core', minimum: 64, maximum: 8192, step: 64, effects: ['generation_quality', 'latency'] },
  temperature: { type: 'number', tier: 'core', minimum: 0, maximum: 2, step: 0.05, effects: ['generation_quality'] },
  top_p: { type: 'number', tier: 'advanced', minimum: 0.01, maximum: 1, step: 0.01, effects: ['generation_quality'] },
}

function copyConfig<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiClientError) return error.message
  return error instanceof Error ? error.message : fallback
}

function formatDate(value: string | null, locale: string): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat(locale === 'ko' ? 'ko-KR' : 'en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function validationTone(state: string): string {
  if (state === 'verified') return 'verified'
  if (state === 'stale' || state === 'unverified') return 'warning'
  return 'neutral'
}

function changedOverrides<T extends Record<string, unknown>>(active: T, candidate: T): Partial<T> {
  return Object.fromEntries(Object.keys(candidate)
    .filter((key) => JSON.stringify(active[key]) !== JSON.stringify(candidate[key]))
    .map((key) => [key, candidate[key]])) as Partial<T>
}

type ProfileSummaryRevision = Pick<QualityProfileRevision<Record<string, unknown>>, 'version' | 'revision' | 'validation' | 'updated_at' | 'note'>

function ProfileSummary({ revision, kind }: { revision: ProfileSummaryRevision; kind: 'active' | 'draft' }) {
  const { locale, t } = useI18n()
  return <div className={`quality-revision ${kind}`}>
    <div><span className={`quality-status ${validationTone(revision.validation.state)}`}>{t(`workspaceSettings.validation.${revision.validation.state}` as TranslationKey)}</span><strong>{kind === 'active' ? t('workspaceSettings.activeVersion', { version: revision.version }) : t('workspaceSettings.draftRevision', { revision: revision.revision })}</strong></div>
    <small>{formatDate(revision.updated_at, locale)}{revision.note ? ` · ${revision.note}` : ''}</small>
  </div>
}

function EffectBadges({ effects = [] }: { effects?: string[] }) {
  const { t } = useI18n()
  return <span className="quality-effects">{effects.map((effect) => <em key={effect}>{t(`workspaceSettings.effect.${effect}` as TranslationKey)}</em>)}</span>
}

interface FieldEditorProps {
  name: string
  value: unknown
  activeValue: unknown
  defaultValue: unknown
  schema: QualityFieldSchema
  disabled: boolean
  onChange: (value: unknown) => void
  onRestore: () => void
  onReset: () => void
}

function FieldEditor({ name, value, activeValue, defaultValue, schema, disabled, onChange, onRestore, onReset }: FieldEditorProps) {
  const { t } = useI18n()
  const isChanged = JSON.stringify(value) !== JSON.stringify(activeValue)
  const inherited = JSON.stringify(value) === JSON.stringify(defaultValue)
  const updateNumber = (event: ChangeEvent<HTMLInputElement>) => onChange(event.target.value === '' ? null : Number(event.target.value))
  const displayValue = name === 'contextual_compression' ? Boolean((value as { enabled?: boolean } | undefined)?.enabled) : value

  return <label className={`quality-field ${isChanged ? 'changed' : ''}`}>
    <span className="quality-field-head"><span><strong>{t(`workspaceSettings.field.${name}` as TranslationKey)}</strong><small>{t(`workspaceSettings.fieldHelp.${name}` as TranslationKey)}</small></span><EffectBadges effects={schema.effects} /></span>
    <span className="quality-input-row">
      {schema.type === 'boolean'
        ? <button type="button" className={`quality-toggle ${displayValue ? 'active' : ''}`} role="switch" aria-checked={Boolean(displayValue)} disabled={disabled} onClick={() => onChange({ enabled: !displayValue })}><i /><span>{displayValue ? t('action.enabled') : t('action.disabled')}</span></button>
        : schema.type === 'enum'
          ? <select disabled={disabled} value={String(value ?? '')} onChange={(event) => onChange(event.target.value)}>{schema.choices?.map((choice) => <option key={choice} value={choice}>{t(`workspaceSettings.choice.${choice}` as TranslationKey)}</option>)}</select>
          : <input disabled={disabled} type="number" value={value == null ? '' : String(value)} min={schema.minimum} max={schema.maximum} step={schema.step} onChange={updateNumber} />}
      <span className="quality-field-actions"><button type="button" className="text-button" disabled={disabled || !isChanged} onClick={onRestore}>{t('workspaceSettings.restoreActive')}</button><button type="button" className="text-button" disabled={disabled || inherited} onClick={onReset}>{t('workspaceSettings.inheritDefault')}</button></span>
    </span>
    <span className="quality-field-meta"><small>{t('workspaceSettings.currentValue', { value: String(activeValue ?? '—') })}</small><small>{t('workspaceSettings.defaultValue', { value: String(defaultValue ?? '—') })}</small>{isChanged && <b>{t('workspaceSettings.changed')}</b>}</span>
  </label>
}

function DraftActions<T extends Record<string, unknown>>({ envelope, busy, note, setNote, allowUnverified, setAllowUnverified, onSave, onDiscard, onApply }: {
  envelope: QualityProfileEnvelope<T>
  busy: boolean
  note: string
  setNote: (value: string) => void
  allowUnverified: boolean
  setAllowUnverified: (value: boolean) => void
  onSave: () => void
  onDiscard: () => void
  onApply: () => void
}) {
  const { t } = useI18n()
  const draft = envelope.draft
  return <aside className="quality-draft-bar">
    <div><strong>{draft ? t('workspaceSettings.draftPending') : t('workspaceSettings.newDraft')}</strong><small>{t('workspaceSettings.draftFlow')}</small></div>
    <label><span>{t('workspaceSettings.changeNote')}</span><input value={note} disabled={busy || !envelope.permissions.can_edit} maxLength={500} placeholder={t('workspaceSettings.changeNotePlaceholder')} onChange={(event) => setNote(event.target.value)} /></label>
    {draft && <label className="quality-unverified"><input type="checkbox" checked={allowUnverified} disabled={busy || !envelope.permissions.can_apply} onChange={(event) => setAllowUnverified(event.target.checked)} /><span>{t('workspaceSettings.allowUnverified')}</span></label>}
    <div className="quality-actions">
      {draft && <button type="button" className="text-button danger" disabled={busy || !envelope.permissions.can_edit} onClick={onDiscard}>{t('workspaceSettings.discard')}</button>}
      <button type="button" className="secondary-button" disabled={busy || !envelope.permissions.can_edit} onClick={onSave}>{busy ? t('workspaceSettings.saving') : t('workspaceSettings.saveDraft')}</button>
      <button type="button" className="primary-button" disabled={busy || !draft || !envelope.permissions.can_apply || (!allowUnverified && draft.validation.state !== 'verified')} onClick={onApply}>{t('workspaceSettings.apply')}</button>
    </div>
  </aside>
}

function GeneralSettingsPanel() {
  const { t } = useI18n()
  const [workspace, setWorkspace] = useState<WorkspaceSummary | null>(null)
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [loadingMembers, setLoadingMembers] = useState(true)
  const [membersError, setMembersError] = useState('')
  const [pendingUserId, setPendingUserId] = useState<number | null>(null)

  const [issuing, setIssuing] = useState(false)
  const [issuedCode, setIssuedCode] = useState('')
  const [codeError, setCodeError] = useState('')
  const [copied, setCopied] = useState(false)

  const [inviteEmail, setInviteEmail] = useState('')
  const [sendingInvite, setSendingInvite] = useState(false)
  const [inviteError, setInviteError] = useState('')
  const [inviteSent, setInviteSent] = useState(false)

  useEffect(() => {
    let active = true
    setLoadingMembers(true)
    Promise.all([tenantApi.currentWorkspace(), tenantApi.listMembers()])
      .then(([currentWorkspace, list]) => {
        if (!active) return
        setWorkspace(currentWorkspace); setMembers(list)
      })
      .catch((err) => { if (active) setMembersError(errorMessage(err, t('workspace.genericError'))) })
      .finally(() => { if (active) setLoadingMembers(false) })
    return () => { active = false }
  }, [t])

  const isAdmin = workspace?.role === 'admin'

  async function changeRole(member: WorkspaceMember, role: 'admin' | 'member') {
    if (pendingUserId !== null || role === member.role) return
    setPendingUserId(member.user_id)
    setMembersError('')
    try {
      const updated = await tenantApi.changeMemberRole(member.user_id, role)
      setMembers((current) => current.map((item) => item.user_id === member.user_id ? updated : item))
    } catch (err) {
      setMembersError(errorMessage(err, t('workspace.genericError')))
    } finally {
      setPendingUserId(null)
    }
  }

  async function removeMember(member: WorkspaceMember) {
    if (pendingUserId !== null || !window.confirm(t('workspace.removeMemberConfirm'))) return
    setPendingUserId(member.user_id)
    setMembersError('')
    try {
      await tenantApi.removeMember(member.user_id)
      setMembers((current) => current.filter((item) => item.user_id !== member.user_id))
    } catch (err) {
      setMembersError(errorMessage(err, t('workspace.genericError')))
    } finally {
      setPendingUserId(null)
    }
  }

  async function issueCode() {
    if (issuing) return
    setIssuing(true)
    setCodeError('')
    setCopied(false)
    try {
      const payload = await tenantApi.issueInviteCode()
      setIssuedCode(payload.code)
    } catch (err) {
      setCodeError(errorMessage(err, t('workspace.genericError')))
    } finally {
      setIssuing(false)
    }
  }

  async function copyCode() {
    if (!issuedCode) return
    try {
      await navigator.clipboard.writeText(issuedCode)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  async function sendInvite(event: FormEvent) {
    event.preventDefault()
    const email = inviteEmail.trim()
    if (!email || sendingInvite) return
    setSendingInvite(true)
    setInviteError('')
    setInviteSent(false)
    try {
      await tenantApi.createInvite(email)
      setInviteSent(true)
      setInviteEmail('')
    } catch (err) {
      setInviteError(errorMessage(err, t('workspace.genericError')))
    } finally {
      setSendingInvite(false)
    }
  }

  if (loadingMembers) return <div className="quality-loading"><Icon name="refresh" /><span>{t('state.loadingTitle')}</span></div>

  return <div className="quality-general">
    <div className="quality-section-head">
      <div><h3>{t('workspaceSettings.generalTitle')}</h3><p>{t('workspaceSettings.generalDescription')}</p></div>
      <span className="quality-axis-note">{t('workspaceSettings.memberCount', { count: members.length })}</span>
    </div>

    {membersError && <div className="settings-error" role="alert">{membersError}</div>}

    <div className="workspace-member-list">
      {members.map((member) => <div key={member.user_id} className="workspace-member-row">
        <span className="share-avatar">{(member.display_name || member.email).slice(0, 1).toUpperCase()}</span>
        <div className="workspace-member-identity"><strong>{member.display_name || member.email}</strong><small>{member.email}</small></div>
        {isAdmin ? <select
          className="workspace-member-role-select"
          value={member.role}
          disabled={pendingUserId === member.user_id}
          onChange={(event) => void changeRole(member, event.target.value as 'admin' | 'member')}
        >
          <option value="admin">{t('workspace.roleAdmin')}</option>
          <option value="member">{t('workspace.roleMember')}</option>
        </select> : <span className="workspace-member-role">{t(member.role === 'admin' ? 'workspace.roleAdmin' : 'workspace.roleMember')}</span>}
        {isAdmin && <button type="button" className="text-button danger" disabled={pendingUserId === member.user_id} onClick={() => void removeMember(member)}>{t('workspace.removeMember')}</button>}
      </div>)}
    </div>

    {isAdmin ? <div className="workspace-invite-grid">
      <div className="workspace-invite-card">
        <h4>{t('workspace.inviteCodeTitle')}</h4>
        <p>{t('workspace.inviteCodeDescription')}</p>
        <div className="workspace-inline-row">
          <button type="button" className="secondary-button" disabled={issuing} onClick={() => void issueCode()}>{issuing ? t('workspace.inviteCodeIssuing') : t('workspace.inviteCodeIssue')}</button>
          {issuedCode && <button type="button" className="secondary-button" onClick={() => void copyCode()}><Icon name={copied ? 'check' : 'copy'} size={14} />{copied ? t('workspace.inviteCodeCopied') : t('workspace.inviteCodeCopy')}</button>}
        </div>
        {issuedCode && <code className="workspace-code">{issuedCode}</code>}
        {issuedCode && <p className="workspace-muted">{t('workspace.inviteCodeHint')}</p>}
        {codeError && <p className="dialog-error" role="alert">{codeError}</p>}
      </div>

      <form className="workspace-invite-card" onSubmit={(event) => void sendInvite(event)}>
        <h4>{t('workspace.inviteByEmailTitle')}</h4>
        <div className="workspace-inline-row">
          <input
            type="email"
            value={inviteEmail}
            disabled={sendingInvite}
            placeholder={t('workspace.inviteByEmailPlaceholder')}
            onChange={(event) => { setInviteEmail(event.target.value); setInviteSent(false) }}
          />
          <button type="submit" className="primary-button" disabled={!inviteEmail.trim() || sendingInvite}>
            {sendingInvite ? t('workspace.inviteByEmailSending') : t('workspace.inviteByEmailSubmit')}
          </button>
        </div>
        {inviteSent && <p className="workspace-muted">{t('workspace.inviteByEmailSent')}</p>}
        {inviteError && <p className="dialog-error" role="alert">{inviteError}</p>}
      </form>
    </div> : <div className="quality-readonly"><Icon name="users" /><span><strong>{t('workspaceSettings.readonlyTitle')}</strong><small>{t('workspace.adminOnlyNotice')}</small></span></div>}
  </div>
}

export function WorkspaceSettingsFeature({ policy, loading: policyLoading, failed: policyFailed }: WorkspaceSettingsFeatureProps) {
  const { locale, t } = useI18n()
  const [tab, setTab] = useState<SettingsTab>('retrieval')
  const [retrieval, setRetrieval] = useState<QualityProfileEnvelope<RetrievalConfig> | null>(null)
  const [generation, setGeneration] = useState<QualityProfileEnvelope<GenerationConfig> | null>(null)
  const [prompt, setPrompt] = useState<PromptProfileEnvelope | null>(null)
  const [versions, setVersions] = useState<QualityProfileVersionSummary[]>([])
  const [form, setForm] = useState<EditableConfig | null>(null)
  const [promptForm, setPromptForm] = useState<PromptPolicy | null>(null)
  const [promptRoute, setPromptRoute] = useState<PromptRoute>('document_rag')
  const [promptPreview, setPromptPreview] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [note, setNote] = useState('')
  const [resetFields, setResetFields] = useState<string[]>([])
  const [allowUnverified, setAllowUnverified] = useState(false)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const [evalDatasets, setEvalDatasets] = useState<EvaluationDataset[]>([])
  const [evalDatasetUid, setEvalDatasetUid] = useState('')
  const [evalCreatingDataset, setEvalCreatingDataset] = useState(false)
  const [evalDatasetName, setEvalDatasetName] = useState('')
  const [evalDatasetItemsText, setEvalDatasetItemsText] = useState('')
  const [evalDatasetError, setEvalDatasetError] = useState('')
  const [evalDatasetBusy, setEvalDatasetBusy] = useState(false)
  const [evalRun, setEvalRun] = useState<EvaluationRun | null>(null)
  const [evalBusy, setEvalBusy] = useState(false)
  const [evalError, setEvalError] = useState('')

  const currentEnvelope = tab === 'retrieval' ? retrieval : tab === 'generation' ? generation : tab === 'prompt_policy' ? prompt : null

  async function loadTab(selected: SettingsTab, alive = () => true) {
    if (selected === 'general') return
    setLoading(true); setError(''); setNotice('')
    try {
      if (selected === 'retrieval') {
        const payload = await retrievalProfileApi.get()
        if (!alive()) return
        setRetrieval(payload); setForm(copyConfig(payload.draft?.effective ?? payload.active.effective)); setNote(payload.draft?.note ?? ''); setResetFields([])
      } else if (selected === 'generation') {
        const payload = await generationProfileApi.get()
        if (!alive()) return
        setGeneration(payload); setForm(copyConfig(payload.draft?.effective ?? payload.active.effective)); setNote(payload.draft?.note ?? ''); setResetFields([])
      } else if (selected === 'prompt_policy') {
        const payload = await promptProfileApi.get()
        if (!alive()) return
        setPrompt(payload); setPromptForm(copyConfig(payload.draft?.effective ?? payload.active.effective)); setNote(payload.draft?.note ?? '')
      } else {
        const [versionsPayload, retrievalPayload, datasetsPayload] = await Promise.all([
          qualityProfileApi.listVersions(),
          retrievalProfileApi.get(),
          evaluationDatasetApi.list('retrieval'),
        ])
        if (!alive()) return
        setVersions(versionsPayload.results)
        setRetrieval(retrievalPayload)
        setEvalDatasets(datasetsPayload.datasets)
        setEvalDatasetUid(''); setEvalCreatingDataset(false); setEvalDatasetName(''); setEvalDatasetItemsText('')
        setEvalDatasetError(''); setEvalRun(null); setEvalError('')
      }
    } catch (loadError) {
      if (alive()) setError(errorMessage(loadError, t('workspaceSettings.loadFailed')))
    } finally { if (alive()) setLoading(false) }
  }

  useEffect(() => {
    let alive = true
    void loadTab(tab, () => alive)
    return () => { alive = false }
    // The selected workspace endpoint is intentionally resolved once per tab.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab])

  const fields = tab === 'retrieval' ? RETRIEVAL_FIELDS : GENERATION_FIELDS
  const editableEnvelope = tab === 'retrieval' ? retrieval : generation
  const effectiveSchema = useMemo(() => ({ ...FALLBACK_SCHEMA, ...(editableEnvelope?.schema ?? {}) }), [editableEnvelope])
  const visibleFields = fields.filter((field) => advanced || effectiveSchema[String(field)]?.tier !== 'advanced')
  const draftConflict = currentEnvelope?.draft_conflict
  const editDisabled = busy || !currentEnvelope?.permissions.can_edit || Boolean(draftConflict)

  function changeField(field: string, value: unknown) {
    if (form) {
      setResetFields((current) => current.filter((item) => item !== field))
      setForm({ ...form, [field]: value } as EditableConfig)
    }
  }

  function inheritField(field: string, value: unknown) {
    if (form) {
      setResetFields((current) => current.includes(field) ? current : [...current, field])
      setForm({ ...form, [field]: value } as EditableConfig)
    }
  }

  function draftOverrides<T extends Record<string, unknown>>(envelope: QualityProfileEnvelope<T>, candidate: T): Partial<T> {
    const keys = new Set([
      ...Object.keys(changedOverrides(envelope.active.effective, candidate)),
      ...(envelope.draft?.changed_fields ?? []),
    ])
    resetFields.forEach((field) => keys.delete(field))
    return Object.fromEntries([...keys].map((field) => [field, candidate[field]])) as Partial<T>
  }

  async function saveConfigDraft() {
    if (!editableEnvelope || !form) return
    const overrides = draftOverrides(editableEnvelope as QualityProfileEnvelope<Record<string, unknown>>, form)
    if (!Object.keys(overrides).length && !resetFields.length) { setNotice(t('workspaceSettings.noChanges')); return }
    setBusy(true); setError(''); setNotice('')
    try {
      if (tab === 'retrieval') await retrievalProfileApi.saveDraft(editableEnvelope.draft?.revision ?? 0, overrides as Partial<RetrievalConfig>, resetFields, note)
      else await generationProfileApi.saveDraft(editableEnvelope.draft?.revision ?? 0, overrides as Partial<GenerationConfig>, resetFields, note)
      await loadTab(tab); setNotice(t('workspaceSettings.draftSaved'))
    } catch (saveError) { setError(errorMessage(saveError, t('workspaceSettings.saveFailed'))) }
    finally { setBusy(false) }
  }

  async function savePromptDraft() {
    if (!prompt || !promptForm) return
    const keys = new Set([
      ...Object.keys(changedOverrides(prompt.active.effective, promptForm)),
      ...(prompt.draft?.changed_fields ?? []),
    ])
    const overrides = Object.fromEntries([...keys].map((route) => [route, promptForm[route]])) as Partial<PromptPolicy>
    if (!Object.keys(overrides).length) { setNotice(t('workspaceSettings.noChanges')); return }
    setBusy(true); setError(''); setNotice(''); setPromptPreview('')
    try {
      await promptProfileApi.saveDraft(prompt.draft?.revision ?? 0, overrides, note)
      await loadTab(tab); setNotice(t('workspaceSettings.draftSaved'))
    } catch (saveError) { setError(errorMessage(saveError, t('workspaceSettings.saveFailed'))) }
    finally { setBusy(false) }
  }

  async function discardDraft() {
    const envelope = currentEnvelope
    if (!envelope?.draft || !window.confirm(t('workspaceSettings.discardConfirm'))) return
    setBusy(true); setError(''); setNotice('')
    try {
      if (tab === 'retrieval') await retrievalProfileApi.discardDraft(envelope.draft.revision)
      else if (tab === 'generation') await generationProfileApi.discardDraft(envelope.draft.revision)
      else if (tab === 'prompt_policy') await promptProfileApi.discardDraft(envelope.draft.revision)
      await loadTab(tab); setNotice(t('workspaceSettings.draftDiscarded'))
    } catch (discardError) { setError(errorMessage(discardError, t('workspaceSettings.discardFailed'))) }
    finally { setBusy(false) }
  }

  async function applyDraft() {
    const envelope = currentEnvelope
    if (!envelope?.draft || !window.confirm(t('workspaceSettings.applyConfirm'))) return
    setBusy(true); setError(''); setNotice('')
    try {
      const runUid = envelope.draft.validation.last_run_uid
      if (tab === 'retrieval') await retrievalProfileApi.apply(envelope.draft.revision, runUid, allowUnverified, note)
      else if (tab === 'generation') await generationProfileApi.apply(envelope.draft.revision, runUid, allowUnverified, note)
      else if (tab === 'prompt_policy') await promptProfileApi.apply(envelope.draft.revision, runUid, allowUnverified, note)
      setAllowUnverified(false); await loadTab(tab); setNotice(t('workspaceSettings.applied'))
    } catch (applyError) { setError(errorMessage(applyError, t('workspaceSettings.applyFailed'))) }
    finally { setBusy(false) }
  }

  async function previewPrompt() {
    if (!prompt?.draft) { setError(t('workspaceSettings.saveBeforePreview')); return }
    setBusy(true); setError(''); setPromptPreview('')
    try {
      const preview = await promptProfileApi.preview(prompt.draft.revision, promptRoute)
      setPromptPreview(preview.assembled_prompt)
    } catch (previewError) { setError(errorMessage(previewError, t('workspaceSettings.previewFailed'))) }
    finally { setBusy(false) }
  }

  async function importPromptFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !promptForm) return
    const extension = file.name.toLowerCase().slice(file.name.lastIndexOf('.'))
    if (!['.txt', '.md'].includes(extension)) {
      setError(t('workspaceSettings.promptFileTypeError'))
      return
    }
    try {
      const instruction = (await file.text()).replace(/^\uFEFF/, '').trim()
      if (!instruction) throw new Error(t('workspaceSettings.promptFileEmpty'))
      if (instruction.length > 12_000) throw new Error(t('workspaceSettings.promptFileTooLarge'))
      setPromptForm({ ...promptForm, [promptRoute]: { mode: 'replace', instruction } })
      setError('')
      setNotice(t('workspaceSettings.promptFileLoaded', { name: file.name }))
      setPromptPreview('')
    } catch (fileError) {
      setError(errorMessage(fileError, t('workspaceSettings.promptFileReadFailed')))
    }
  }

  function selectTab(next: SettingsTab) {
    setAdvanced(false); setPromptPreview(''); setAllowUnverified(false); setTab(next)
  }

  useEffect(() => {
    if (!evalRun || evalRun.status === 'succeeded' || evalRun.status === 'failed') return
    let alive = true
    const timer = setInterval(() => {
      evaluationRunApi.get(evalRun.uid).then((result) => {
        if (!alive) return
        setEvalRun(result.run)
        if (result.run.status === 'succeeded') {
          retrievalProfileApi.get().then((refreshed) => { if (alive) setRetrieval(refreshed) }).catch(() => {})
        }
      }).catch(() => {})
    }, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [evalRun])

  async function createEvaluationDataset() {
    if (evalDatasetBusy) return
    setEvalDatasetBusy(true); setEvalDatasetError('')
    try {
      if (!evalDatasetName.trim()) throw new Error(t('workspaceSettings.evalDatasetNameRequired'))
      let items: unknown
      try { items = JSON.parse(evalDatasetItemsText) } catch { throw new Error(t('workspaceSettings.evalDatasetInvalidJson')) }
      const result = await evaluationDatasetApi.create('retrieval', evalDatasetName.trim(), items as never)
      setEvalDatasets((current) => [result.dataset, ...current])
      setEvalDatasetUid(result.dataset.uid)
      setEvalCreatingDataset(false); setEvalDatasetName(''); setEvalDatasetItemsText('')
    } catch (datasetError) {
      setEvalDatasetError(errorMessage(datasetError, t('workspaceSettings.evalDatasetSaveFailed')))
    } finally { setEvalDatasetBusy(false) }
  }

  async function runEvaluation() {
    if (!retrieval?.draft || !evalDatasetUid || evalBusy) return
    setEvalBusy(true); setEvalError(''); setEvalRun(null)
    try {
      const started = await evaluationRunApi.startRetrieval(retrieval.draft.revision, evalDatasetUid)
      setEvalRun(started.run)
    } catch (runError) {
      setEvalError(errorMessage(runError, t('workspaceSettings.evalRunFailed')))
    } finally { setEvalBusy(false) }
  }

  return <section className="panel settings-panel quality-settings">
    <div className="panel-title quality-heading"><div><span className="eyebrow">{t('workspaceSettings.eyebrow')}</span><h2>{t('workspaceSettings.title')}</h2><p>{t('workspaceSettings.description')}</p></div>{policy && <span className="quality-runtime"><i className={`status-dot ${policy.rag.available ? '' : 'warn'}`} />{policyLoading ? t('state.loadingTitle') : t('workspaceSettings.runtimeManaged')}</span>}</div>
    {(policyFailed || error) && <div className="settings-error" role="alert">{error || t('workspaceSettings.loadFailed')}</div>}
    {notice && <div className="quality-notice" role="status"><Icon name="check" size={14} />{notice}</div>}

    <nav className="quality-tabs" aria-label={t('workspaceSettings.sections')}>{(['general', 'retrieval', 'generation', 'prompt_policy', 'evaluation'] as SettingsTab[]).map((key) => <button key={key} type="button" className={tab === key ? 'active' : ''} onClick={() => selectTab(key)}>{t(`workspaceSettings.tab.${key}` as TranslationKey)}</button>)}</nav>

    {tab === 'general' ? <GeneralSettingsPanel />
      : loading ? <div className="quality-loading"><Icon name="refresh" /><span>{t('state.loadingTitle')}</span></div>
      : tab === 'evaluation' ? <div className="quality-evaluation">
        <div className="quality-section-head"><div><h3>{t('workspaceSettings.evaluationTitle')}</h3><p>{t('workspaceSettings.evaluationDescription')}</p></div></div>
        <div className="quality-flow"><span><b>1</b>{t('workspaceSettings.flowDataset')}</span><i /><span><b>2</b>{t('workspaceSettings.flowDraft')}</span><i /><span><b>3</b>{t('workspaceSettings.flowCompare')}</span><i /><span><b>4</b>{t('workspaceSettings.flowApply')}</span></div>
        <p className="quality-axis-note-line">{t('workspaceSettings.evalRetrievalOnlyNote')}</p>

        {!retrieval?.draft ? <div className="quality-callout">
          <Icon name="sparkles" />
          <div><strong>{t('workspaceSettings.evalNeedsDraftTitle')}</strong><p>{t('workspaceSettings.evalNeedsDraftDescription')}</p></div>
          <button type="button" className="secondary-button" onClick={() => selectTab('retrieval')}>{t('workspaceSettings.openDraft')}</button>
        </div> : <div className="quality-eval-config">
          <div className="quality-eval-dataset-row">
            <label><span>{t('workspaceSettings.evalDatasetLabel')}</span>
              <select value={evalDatasetUid} onChange={(event) => setEvalDatasetUid(event.target.value)}>
                <option value="">{t('workspaceSettings.evalDatasetPlaceholder')}</option>
                {evalDatasets.map((dataset) => <option key={dataset.uid} value={dataset.uid}>{dataset.name} ({dataset.item_count})</option>)}
              </select>
            </label>
            <button type="button" className="text-button" onClick={() => setEvalCreatingDataset((value) => !value)}>{t('workspaceSettings.evalNewDataset')}</button>
          </div>

          {evalCreatingDataset && <div className="quality-eval-dataset-form">
            <label><span>{t('workspaceSettings.evalDatasetName')}</span><input value={evalDatasetName} maxLength={200} onChange={(event) => setEvalDatasetName(event.target.value)} /></label>
            <label><span>{t('workspaceSettings.evalDatasetItems')}</span><textarea value={evalDatasetItemsText} placeholder={t('workspaceSettings.evalDatasetItemsPlaceholder')} onChange={(event) => setEvalDatasetItemsText(event.target.value)} /></label>
            <small>{t('workspaceSettings.evalDatasetItemsHelp')}</small>
            {evalDatasetError && <p className="dialog-error" role="alert">{evalDatasetError}</p>}
            <div className="quality-eval-dataset-actions"><button type="button" className="secondary-button" disabled={evalDatasetBusy} onClick={() => void createEvaluationDataset()}>{evalDatasetBusy ? t('workspaceSettings.saving') : t('workspaceSettings.evalDatasetSave')}</button></div>
          </div>}

          <button type="button" className="primary-button" disabled={!evalDatasetUid || evalBusy} onClick={() => void runEvaluation()}>{evalBusy ? t('workspaceSettings.evalRunning') : t('workspaceSettings.evalRun')}</button>
          {evalError && <p className="dialog-error" role="alert">{evalError}</p>}
        </div>}

        {evalRun && <div className="quality-eval-result">
          <span className={`quality-status ${evalRun.status === 'succeeded' ? 'verified' : evalRun.status === 'failed' ? 'warning' : 'neutral'}`}>{t(`workspaceSettings.evalStatus.${evalRun.status}` as TranslationKey)}</span>
          {evalRun.status === 'succeeded' && <div className="quality-eval-metrics">
            <div><strong>{evalRun.metrics.hit_rate_at_1}</strong><small>{t('workspaceSettings.evalMetricHitAt1')}</small></div>
            <div><strong>{evalRun.metrics.hit_rate_at_k}</strong><small>{t('workspaceSettings.evalMetricHitAtK')}</small></div>
            <div><strong>{evalRun.metrics.mrr_at_k}</strong><small>{t('workspaceSettings.evalMetricMrr')}</small></div>
            <div><strong>{evalRun.metrics.queries}</strong><small>{t('workspaceSettings.evalMetricQueries')}</small></div>
            <div><strong>{evalRun.metrics.chunk_count}</strong><small>{t('workspaceSettings.evalMetricChunks')}</small></div>
          </div>}
          {evalRun.status === 'succeeded' && <p className="quality-eval-verified-note">{t('workspaceSettings.evalVerifiedNotice')}</p>}
          {evalRun.status === 'succeeded' && <button type="button" className="secondary-button" onClick={() => selectTab('retrieval')}>{t('workspaceSettings.evalGoApply')}</button>}
          {evalRun.status === 'failed' && <p className="dialog-error" role="alert">{evalRun.error_message}</p>}
        </div>}

        <div className="quality-section-head versions"><div><h3>{t('workspaceSettings.historyTitle')}</h3><p>{t('workspaceSettings.historyDescription')}</p></div></div>
        <div className="quality-version-list">{versions.length ? versions.map((version) => <div key={version.uid}><span><b>v{version.version}</b><em>{t(`workspaceSettings.tab.${version.change_axis}` as TranslationKey)}</em></span><span><strong>{version.note || t('workspaceSettings.noNote')}</strong><small>{formatDate(version.applied_at, locale)}</small></span><span className={`quality-status ${validationTone(version.validation.state)}`}>{t(`workspaceSettings.validation.${version.validation.state}` as TranslationKey)}</span></div>) : <p>{t('workspaceSettings.noHistory')}</p>}</div>
      </div>
      : currentEnvelope ? <>
        <div className="quality-revision-grid"><ProfileSummary revision={currentEnvelope.active} kind="active" />{currentEnvelope.draft ? <ProfileSummary revision={currentEnvelope.draft} kind="draft" /> : <div className="quality-revision empty"><strong>{t('workspaceSettings.noDraft')}</strong><small>{t('workspaceSettings.noDraftDescription')}</small></div>}</div>
        {draftConflict && <div className="quality-conflict"><Icon name="clock" /><div><strong>{t('workspaceSettings.draftConflict', { axis: t(`workspaceSettings.tab.${draftConflict.change_axis}` as TranslationKey) })}</strong><p>{t('workspaceSettings.draftConflictDescription')}</p></div><button type="button" className="secondary-button" onClick={() => selectTab(draftConflict.change_axis)}>{t('workspaceSettings.openDraft')}</button></div>}
        {!currentEnvelope.permissions.can_edit && <div className="quality-readonly"><Icon name="users" /><span><strong>{t('workspaceSettings.readonlyTitle')}</strong><small>{t('workspaceSettings.readonlyDescription')}</small></span></div>}

        {tab === 'prompt_policy' && prompt && promptForm ? <div className="quality-prompt">
          <div className="quality-section-head"><div><h3>{t('workspaceSettings.promptTitle')}</h3><p>{t('workspaceSettings.promptDescription')}</p></div></div>
          <div className="prompt-route-tabs">{(['document_rag', 'no_retrieval'] as PromptRoute[]).map((route) => <button type="button" className={promptRoute === route ? 'active' : ''} onClick={() => { setPromptRoute(route); setPromptPreview('') }} key={route}>{t(`workspaceSettings.promptRoute.${route}` as TranslationKey)}</button>)}</div>
          <div className="fixed-contract-note"><Icon name="layers" size={16} /><span><strong>{t('workspaceSettings.fixedContract')}</strong><small>{prompt.fixed_contract || t('workspaceSettings.fixedContractDescription')}</small></span></div>
          <div className="prompt-mode-row"><label><input type="radio" name="prompt-mode" value="inherit" checked={promptForm[promptRoute].mode === 'inherit'} disabled={editDisabled} onChange={() => setPromptForm({ ...promptForm, [promptRoute]: { mode: 'inherit', instruction: null } })} /><span><strong>{t('workspaceSettings.promptInherit')}</strong><small>{t('workspaceSettings.promptInheritDescription')}</small></span></label><label><input type="radio" name="prompt-mode" value="replace" checked={promptForm[promptRoute].mode === 'replace'} disabled={editDisabled} onChange={() => setPromptForm({ ...promptForm, [promptRoute]: { mode: 'replace', instruction: promptForm[promptRoute].instruction || '' } })} /><span><strong>{t('workspaceSettings.promptReplace')}</strong><small>{t('workspaceSettings.promptReplaceDescription')}</small></span></label></div>
          <div className="prompt-import-row"><label className={`secondary-button ${editDisabled ? 'disabled' : ''}`}><Icon name="upload" size={14} />{t('workspaceSettings.promptImport')}<input type="file" accept=".txt,.md,text/plain,text/markdown" disabled={editDisabled} onChange={(event) => void importPromptFile(event)} /></label><small>{t('workspaceSettings.promptImportDescription')}</small></div>
          {promptForm[promptRoute].mode === 'replace' && <label className="prompt-editor"><span><strong>{t('workspaceSettings.workspaceInstruction')}</strong><small>{(promptForm[promptRoute].instruction || '').length} / 12,000</small></span><textarea disabled={editDisabled} maxLength={12000} value={promptForm[promptRoute].instruction || ''} placeholder={t('workspaceSettings.promptPlaceholder')} onChange={(event) => setPromptForm({ ...promptForm, [promptRoute]: { mode: 'replace', instruction: event.target.value } })} /></label>}
          {prompt.provider_disclosure && <p className="provider-disclosure">{prompt.provider_disclosure}</p>}
          <div className="prompt-preview-actions"><button type="button" className="secondary-button" disabled={busy || !prompt.draft} onClick={() => void previewPrompt()}>{t('workspaceSettings.preview')}</button><small>{t('workspaceSettings.previewDescription')}</small></div>
          {promptPreview && <pre className="prompt-preview">{promptPreview}</pre>}
          <DraftActions envelope={prompt} busy={busy} note={note} setNote={setNote} allowUnverified={allowUnverified} setAllowUnverified={setAllowUnverified} onSave={() => void savePromptDraft()} onDiscard={() => void discardDraft()} onApply={() => void applyDraft()} />
        </div>
          : editableEnvelope && form ? <div className="quality-profile-editor">
            <div className="quality-section-head"><div><h3>{t(`workspaceSettings.${tab}Title` as TranslationKey)}</h3><p>{t(`workspaceSettings.${tab}Description` as TranslationKey)}</p></div><span className="quality-axis-note">{t('workspaceSettings.oneAxisOnly')}</span></div>
            <div className="quality-field-grid">{visibleFields.map((field) => <FieldEditor key={String(field)} name={String(field)} value={form[field]} activeValue={editableEnvelope.active.effective[field]} defaultValue={editableEnvelope.defaults[field]} schema={effectiveSchema[String(field)]} disabled={editDisabled} onChange={(value) => changeField(String(field), value)} onRestore={() => changeField(String(field), editableEnvelope.active.effective[field])} onReset={() => inheritField(String(field), editableEnvelope.defaults[field])} />)}</div>
            <button type="button" className="advanced-toggle" onClick={() => setAdvanced((value) => !value)}><Icon name="chevron" size={13} />{advanced ? t('workspaceSettings.hideAdvanced') : t('workspaceSettings.showAdvanced')}</button>
            {tab === 'retrieval' && Number(form.dense_weight) + Number(form.sparse_weight) !== 1 && <p className="quality-inline-warning">{t('workspaceSettings.weightWarning')}</p>}
            {tab === 'retrieval' && retrieval
              ? <DraftActions envelope={retrieval} busy={busy} note={note} setNote={setNote} allowUnverified={allowUnverified} setAllowUnverified={setAllowUnverified} onSave={() => void saveConfigDraft()} onDiscard={() => void discardDraft()} onApply={() => void applyDraft()} />
              : tab === 'generation' && generation
                ? <DraftActions envelope={generation} busy={busy} note={note} setNote={setNote} allowUnverified={allowUnverified} setAllowUnverified={setAllowUnverified} onSave={() => void saveConfigDraft()} onDiscard={() => void discardDraft()} onApply={() => void applyDraft()} />
                : null}
          </div> : null}
      </>
        : <div className="quality-empty"><Icon name="settings" /><strong>{t('workspaceSettings.apiUnavailable')}</strong><p>{t('workspaceSettings.apiUnavailableDescription')}</p></div>}
  </section>
}
