import { type FormEvent, useEffect, useRef, useState } from 'react'
import { ApiClientError } from '../../api/http'
import { llmSettingsApi, type LLMSettings } from '../../api/llmSettings'
import type { ChatPhase, RagCitation, RagConversation, RagConversationMessage, RagReplay, RagStarted, SearchResult, SearchScopeNode } from '../../api/models'
import { workspaceApi } from '../../api/workspace'
import { Icon } from '../../components/Icon'
import { ResultCard } from '../../components/ResultCard'
import { useI18n } from '../../i18n'

function citationResult(citation: RagCitation): SearchResult {
  const score = citation.docScore ?? citation.hybridScore ?? citation.denseScore ?? 0
  const fileType = (citation.nodeName.split('.').pop() || 'FILE').replace('.', '').toUpperCase()
  return {
    uid: `${citation.nodeId}:${citation.id}`,
    title: citation.nodeName || citation.nodeId,
    fileType,
    score,
    text: citation.text,
    page: citation.pages,
    section: citation.section,
    evidences: [{
      chunkId: citation.chunkId ?? 0,
      text: citation.text,
      contextText: citation.text,
      section: citation.section,
      pages: citation.pages,
      score: citation.hybridScore ?? citation.docScore,
    }],
  }
}

function durationMetric(metrics: Record<string, unknown>): number | null {
  for (const key of ['end_to_end_ms', 'worker_total_ms', 'llm_total_ms', 'total_ms']) {
    if (typeof metrics[key] === 'number') return Math.round(metrics[key])
  }
  return null
}

const COMPOSE_MAX_HEIGHT = 120

function autoGrow(el: HTMLTextAreaElement) {
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, COMPOSE_MAX_HEIGHT)}px`
}

interface ChatFeatureProps {
  conversationUid: string | null
  onConversationChange: (uid: string | null) => void
  onOpenDocument: (uid: string) => void
}

export function ChatFeature({ conversationUid, onConversationChange, onOpenDocument }: ChatFeatureProps) {
  const { t, locale } = useI18n()
  const [question, setQuestion] = useState('')
  const [submittedQuestion, setSubmittedQuestion] = useState('')
  const [answer, setAnswer] = useState('')
  const [citations, setCitations] = useState<RagCitation[]>([])
  const [phase, setPhase] = useState<ChatPhase>('idle')
  const [error, setError] = useState<Error | null>(null)
  const [started, setStarted] = useState<RagStarted | null>(null)
  const [metrics, setMetrics] = useState<Record<string, unknown>>({})
  const [scopes, setScopes] = useState<SearchScopeNode[]>([])
  const [selectedNodeIds, setSelectedNodeIds] = useState<string[]>([])
  const [scopeLoading, setScopeLoading] = useState(true)
  const [scopeFailed, setScopeFailed] = useState(false)
  const [conversations, setConversations] = useState<RagConversation[]>([])
  const [activeConversation, setActiveConversation] = useState<RagConversation | null>(null)
  const [messages, setMessages] = useState<RagConversationMessage[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyFailed, setHistoryFailed] = useState(false)
  const [scopeOpen, setScopeOpen] = useState(false)
  const [llmSettings, setLlmSettings] = useState<LLMSettings | null>(null)
  const [llmOpen, setLlmOpen] = useState(false)
  const [llmSaving, setLlmSaving] = useState(false)
  const [llmError, setLlmError] = useState(false)
  const [manageOpen, setManageOpen] = useState(false)
  const [manageTitle, setManageTitle] = useState('')
  const [manageNodeIds, setManageNodeIds] = useState<string[]>([])
  const [manageSaving, setManageSaving] = useState(false)
  const [manageError, setManageError] = useState<Error | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const cancelRef = useRef<() => void>(() => undefined)
  const cancelRequestedRef = useRef(false)
  const activeRequestIdRef = useRef<string | null>(null)
  const skipConversationLoadRef = useRef<string | null>(null)
  const scopeAnchorRef = useRef<HTMLDivElement>(null)
  const llmAnchorRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const busy = phase === 'searching' || phase === 'preparing' || phase === 'streaming'
  const readOnlyConversation = conversationUid !== null && !activeConversation?.canManage

  useEffect(() => {
    let active = true
    workspaceApi.listSearchScopes()
      .then((nodes) => { if (active) setScopes(nodes) })
      .catch(() => { if (active) setScopeFailed(true) })
      .finally(() => { if (active) setScopeLoading(false) })
    workspaceApi.listRagConversations()
      .then((items) => { if (active) setConversations(items) })
      .catch(() => { if (active) setHistoryFailed(true) })
      .finally(() => { if (active) setHistoryLoading(false) })
    llmSettingsApi.get()
      .then((settings) => { if (active) setLlmSettings(settings) })
      .catch(() => undefined)
    return () => {
      active = false
      cancelRef.current()
    }
  }, [])

  useEffect(() => {
    let active = true
    if (conversationUid && skipConversationLoadRef.current === conversationUid) {
      skipConversationLoadRef.current = null
      return () => { active = false }
    }
    activeRequestIdRef.current = null
    cancelRequestedRef.current = false
    cancelRef.current()
    cancelRef.current = () => undefined
    setSubmittedQuestion('')
    setAnswer('')
    setCitations([])
    setError(null)
    setStarted(null)
    setMetrics({})
    setPhase('idle')
    setActiveConversation(null)
    if (!conversationUid) {
      setMessages([])
      return () => { active = false }
    }
    Promise.all([
      workspaceApi.getRagConversation(conversationUid),
      workspaceApi.listRagConversationMessages(conversationUid),
    ]).then(([conversation, items]) => {
      if (!active) return
      setActiveConversation(conversation)
      setMessages(items)
      setSelectedNodeIds(conversation.defaultNodeIds)
      const latestAssistant = [...items].reverse().find((message) => message.role === 'assistant' && message.ragJob)
      if (latestAssistant?.ragJob) {
        setCitations(latestAssistant.ragJob.citations)
        setMetrics(latestAssistant.ragJob.performanceMetrics)
      }
    }).catch((loadError) => {
      if (active) setError(loadError instanceof Error ? loadError : new Error(String(loadError)))
    })
    return () => { active = false }
  }, [conversationUid])

  useEffect(() => {
    if (textareaRef.current) autoGrow(textareaRef.current)
  }, [question])

  useEffect(() => {
    if (!scopeOpen && !llmOpen) return
    function onPointerDown(event: MouseEvent) {
      if (scopeOpen && scopeAnchorRef.current && !scopeAnchorRef.current.contains(event.target as Node)) setScopeOpen(false)
      if (llmOpen && llmAnchorRef.current && !llmAnchorRef.current.contains(event.target as Node)) setLlmOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      setScopeOpen(false)
      setLlmOpen(false)
    }
    window.addEventListener('mousedown', onPointerDown)
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('mousedown', onPointerDown)
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [scopeOpen, llmOpen])

  const statusText = phase === 'searching'
    ? t('chat.status.searching')
    : phase === 'preparing'
      ? t('chat.status.preparing', { count: citations.length })
      : phase === 'streaming'
        ? t('chat.status.streaming')
        : phase === 'done'
          ? t('chat.status.done')
          : phase === 'canceled'
            ? t('chat.status.canceled')
            : phase === 'error'
              ? t('chat.status.error')
              : t('chat.status.idle')

  function errorMessage(current: Error): string {
    if (!(current instanceof ApiClientError)) return current.message
    if (current.code === 'RAG_CAPACITY_EXCEEDED') {
      return current.retryAfterSeconds === null
        ? t('chat.capacity')
        : t('chat.capacityRetry', { seconds: current.retryAfterSeconds })
    }
    if (current.code === 'LLM_RUNTIME_UNAVAILABLE') return t('chat.runtimeUnavailable')
    if (current.code === 'RAG_STREAM_INCOMPLETE') return t('chat.streamIncomplete')
    if (current.code === 'RAG_GENERATION_FAILED') return t('chat.generationFailed')
    if (current.code === 'SEARCH_FAILED') return t('chat.searchFailed')
    if (current.code === 'CONVERSATION_BUSY') return t('chat.conversationBusy')
    return current.message
  }

  async function startRequest(questionOverride?: string, nodeIdsOverride?: string[]) {
    const nextQuestion = (questionOverride ?? question).trim()
    if (!nextQuestion) return
    const requestNodeIds = nodeIdsOverride ?? selectedNodeIds
    let targetConversationUid = conversationUid
    if (!targetConversationUid) {
      try {
        const created = await workspaceApi.createRagConversation(nextQuestion.slice(0, 160), requestNodeIds)
        targetConversationUid = created.uid
        setActiveConversation(created)
        skipConversationLoadRef.current = created.uid
        setConversations((current) => [created, ...current.filter((item) => item.uid !== created.uid)])
        onConversationChange(created.uid)
      } catch (createError) {
        setPhase('error')
        setError(createError instanceof Error ? createError : new Error(String(createError)))
        return
      }
    }
    cancelRef.current()
    setSubmittedQuestion(nextQuestion)
    setAnswer('')
    setCitations([])
    setError(null)
    setStarted(null)
    setMetrics({})
    const requestId = crypto.randomUUID()
    cancelRequestedRef.current = false
    activeRequestIdRef.current = requestId
    const ifCurrentRequest = <T,>(callback: (value: T) => void) => (value: T) => {
      if (activeRequestIdRef.current === requestId) callback(value)
    }
    cancelRef.current = workspaceApi.streamAnswer({
      question: nextQuestion,
      language: locale,
      nodeIds: requestNodeIds,
      conversationUid: targetConversationUid,
      clientRequestId: requestId,
    }, {
      onPhase: ifCurrentRequest(setPhase),
      onStarted: ifCurrentRequest(setStarted),
      onSources: ifCurrentRequest(setCitations),
      onToken: ifCurrentRequest((token: string) => setAnswer((current) => current + token)),
      onCompleted: (completed) => {
        if (activeRequestIdRef.current !== requestId) return
        setAnswer(completed.answer)
        setCitations(completed.citations)
        setMetrics(completed.performanceMetrics)
        workspaceApi.listRagConversationMessages(targetConversationUid as string)
          .then((items) => {
            if (activeRequestIdRef.current !== requestId) return
            setMessages(items)
            setSubmittedQuestion('')
          })
          .catch(() => { if (activeRequestIdRef.current === requestId) setHistoryFailed(true) })
        workspaceApi.listRagConversations()
          .then((items) => { if (activeRequestIdRef.current === requestId) setConversations(items) })
          .catch(() => { if (activeRequestIdRef.current === requestId) setHistoryFailed(true) })
      },
      onReplay: (replay: RagReplay) => {
        if (activeRequestIdRef.current !== requestId) return
        setAnswer(replay.answer)
        setCitations(replay.citations)
        setMetrics(replay.performanceMetrics)
        if (replay.status === 'completed') {
          setPhase('done')
          workspaceApi.listRagConversationMessages(targetConversationUid as string)
            .then((items) => {
              if (activeRequestIdRef.current !== requestId) return
              setMessages(items)
              setSubmittedQuestion('')
            })
            .catch(() => { if (activeRequestIdRef.current === requestId) setHistoryFailed(true) })
          workspaceApi.listRagConversations()
            .then((items) => { if (activeRequestIdRef.current === requestId) setConversations(items) })
            .catch(() => { if (activeRequestIdRef.current === requestId) setHistoryFailed(true) })
        } else if (replay.status === 'canceled') {
          setPhase('canceled')
          activeRequestIdRef.current = null
        } else if (replay.status === 'failed' || replay.status === 'interrupted') {
          setPhase('error')
          setError(new ApiClientError(500, 'RAG_REPLAY_TERMINAL', replay.errorMessage || 'The previous RAG request did not complete.'))
          activeRequestIdRef.current = null
        } else {
          setPhase('preparing')
        }
      },
      onCanceled: ifCurrentRequest((performanceMetrics: Record<string, unknown>) => {
        setMetrics(performanceMetrics)
        activeRequestIdRef.current = null
      }),
      onError: ifCurrentRequest((streamError: Error) => {
        setError(streamError)
        if (!(streamError instanceof ApiClientError) || streamError.code !== 'RAG_CAPACITY_EXCEEDED') {
          activeRequestIdRef.current = null
          return
        }
        // The server reserved and persisted this failed turn before admission. Reload it
        // so the retry action is attached to the durable message instead of an optimistic
        // duplicate rendered by the active request block.
        workspaceApi.listRagConversationMessages(targetConversationUid as string)
          .then((items) => {
            if (activeRequestIdRef.current !== requestId) return
            setMessages(items)
            setSubmittedQuestion('')
            setAnswer('')
            setCitations([])
            activeRequestIdRef.current = null
          })
          .catch(() => { if (activeRequestIdRef.current === requestId) setHistoryFailed(true) })
      }),
    })
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (busy) {
      cancel()
      return
    }
    startRequest()
  }

  function cancel() {
    activeRequestIdRef.current = null
    if (conversationUid) {
      cancelRequestedRef.current = true
      void workspaceApi.cancelRagConversation(conversationUid).catch(() => undefined)
    }
    cancelRef.current()
    cancelRef.current = () => undefined
    setPhase('canceled')
  }

  async function selectLlmEndpoint(endpointId: number | null) {
    if (llmSaving) return
    setLlmSaving(true)
    setLlmError(false)
    try {
      const updated = await llmSettingsApi.select(endpointId)
      setLlmSettings(updated)
      setLlmOpen(false)
    } catch {
      setLlmError(true)
    } finally {
      setLlmSaving(false)
    }
  }

  function toggleScope(uid: string) {
    setSelectedNodeIds((current) => current.includes(uid)
      ? current.filter((value) => value !== uid)
      : [...current, uid])
  }

  function openConversationManager() {
    if (!activeConversation?.canManage || busy) return
    setManageTitle(activeConversation.title)
    setManageNodeIds(activeConversation.defaultNodeIds)
    setManageError(null)
    setDeleteConfirm(false)
    setManageOpen(true)
  }

  function toggleManageScope(uid: string) {
    setManageNodeIds((current) => current.includes(uid)
      ? current.filter((value) => value !== uid)
      : [...current, uid])
  }

  async function saveConversation(event: FormEvent) {
    event.preventDefault()
    if (!activeConversation || manageSaving || !manageTitle.trim()) return
    setManageSaving(true)
    setManageError(null)
    try {
      const updated = await workspaceApi.updateRagConversation(
        activeConversation.uid,
        activeConversation.revision,
        { title: manageTitle.trim(), defaultNodeIds: manageNodeIds },
      )
      setActiveConversation(updated)
      setSelectedNodeIds(updated.defaultNodeIds)
      setConversations((current) => current.map((item) => item.uid === updated.uid ? updated : item))
      setManageOpen(false)
    } catch (saveError) {
      setManageError(saveError instanceof Error ? saveError : new Error(String(saveError)))
    } finally {
      setManageSaving(false)
    }
  }

  async function deleteConversation() {
    if (!activeConversation || manageSaving) return
    setManageSaving(true)
    setManageError(null)
    try {
      try {
        await workspaceApi.deleteRagConversation(activeConversation.uid)
      } catch (deleteError) {
        if (!(deleteError instanceof ApiClientError) || deleteError.code !== 'CONVERSATION_BUSY' || !cancelRequestedRef.current) throw deleteError
        let idle = false
        for (let attempt = 0; attempt < 12; attempt += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 250))
          const items = await workspaceApi.listRagConversationMessages(activeConversation.uid)
          idle = !items.some((message) => message.ragJob && ['pending', 'processing'].includes(message.ragJob.status))
          if (idle) break
        }
        if (!idle) throw deleteError
        await workspaceApi.deleteRagConversation(activeConversation.uid)
      }
      cancelRef.current()
      cancelRef.current = () => undefined
      setConversations((current) => current.filter((item) => item.uid !== activeConversation.uid))
      setManageOpen(false)
      setDeleteConfirm(false)
      setActiveConversation(null)
      onConversationChange(null)
    } catch (deleteError) {
      setManageError(deleteError instanceof Error ? deleteError : new Error(String(deleteError)))
    } finally {
      setManageSaving(false)
    }
  }

  function scrollToCitation(id: number) {
    document.getElementById(`rag-citation-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  function historyDate(item: RagConversation): string {
    const date = new Date(item.updatedAt)
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
  }

  function persistedStatusText(status: string | undefined): string {
    if (status === 'completed') return t('chat.status.done')
    if (status === 'pending') return t('chat.status.pending')
    if (status === 'processing') return t('chat.status.processing')
    if (status === 'canceled') return t('chat.status.canceled')
    if (status === 'interrupted') return t('chat.status.interrupted')
    if (status === 'failed') return t('chat.status.failed')
    return status || t('chat.status.error')
  }

  const duration = durationMetric(metrics)

  return <div className="chat-layout">
    <section className="panel chat-panel">
      <div className="chat-scroll">
        {messages.length === 0 && phase === 'idle' && <div className="chat-welcome"><span className="ai-orb large"><Icon name="sparkles" size={23}/></span><h2>{t('chat.askTitle')}</h2><p>{t('chat.askDescription')}</p><div className="suggestions"><button onClick={() => setQuestion(t('chat.suggestionPolicy'))}>{t('chat.suggestionPolicy')}</button><button onClick={() => setQuestion(t('chat.suggestionStrategy'))}>{t('chat.suggestionStrategy')}</button></div></div>}
        {messages.map((message) => message.role === 'user'
          ? <div className="message user-message" key={message.uid}>{message.content}</div>
          : <div className={`message assistant-message ${['failed', 'interrupted'].includes(message.ragJob?.status ?? '') ? 'has-error' : ''}`} key={message.uid}>
              <div className="answer-status"><span className={['pending', 'processing'].includes(message.ragJob?.status ?? '') ? 'pulse-dot' : 'complete-dot'}>{message.ragJob?.status === 'completed' && <Icon name="check" size={11}/>}</span>{persistedStatusText(message.ragJob?.status)}</div>
              {message.ragJob?.answer && <p className="answer-text">{message.ragJob.answer}</p>}
              {message.ragJob?.errorMessage && <div className="chat-error"><p>{message.ragJob.errorMessage}</p></div>}
              {message.ragJob && ['failed', 'interrupted', 'canceled'].includes(message.ragJob.status) && (() => {
                const retryQuestion = messages.find((candidate) => candidate.uid === message.replyToUid && candidate.role === 'user')
                return retryQuestion ? <button type="button" className="chat-retry" onClick={() => startRequest(retryQuestion.content, message.nodeIds)}><Icon name="refresh" size={13}/>{t('chat.retry')}</button> : null
              })()}
              {message.ragJob && message.ragJob.citations.length > 0 && <div className="citation-row">{message.ragJob.citations.map((citation) => <button type="button" key={citation.id} onClick={() => setCitations(message.ragJob?.citations ?? [])}>[{citation.id}] {citation.nodeName}</button>)}</div>}
            </div>
        )}
        {submittedQuestion && <>
          <div className="message user-message">{submittedQuestion}</div>
          <div className={`message assistant-message ${phase === 'error' ? 'has-error' : ''}`}>
            <div className="answer-status"><span className={busy ? 'pulse-dot' : 'complete-dot'}>{phase === 'done' && <Icon name="check" size={11}/>}</span>{statusText}</div>
            {answer && <p className="answer-text">{answer}</p>}
            {error && <div className="chat-error"><p>{errorMessage(error)}</p><button type="button" onClick={() => void startRequest()}><Icon name="refresh" size={13}/>{t('chat.retry')}</button></div>}
            {phase === 'canceled' && <button type="button" className="chat-retry" onClick={() => void startRequest()}><Icon name="refresh" size={13}/>{t('chat.retry')}</button>}
            {phase === 'done' && citations.length > 0 && <div className="citation-row">{citations.map((citation) => <button type="button" key={citation.id} onClick={() => scrollToCitation(citation.id)}>[{citation.id}] {citation.nodeName}</button>)}</div>}
            {phase === 'done' && <div className="chat-meta">{started?.llmModel && <span>{t('chat.model', { model: started.llmModel })}</span>}{duration !== null && <span>{t('chat.duration', { duration })}</span>}</div>}
          </div>
        </>}
      </div>
      {readOnlyConversation && <p className="chat-read-only">{t('chat.readOnly')}</p>}
      <form className="chat-compose" onSubmit={submit}>
        <div className="chat-compose-chips">
          <div className="chat-compose-tool-group" ref={scopeAnchorRef}>
            <button type="button" className={`compose-chip ${selectedNodeIds.length ? 'active' : ''}`} onClick={() => setScopeOpen((value) => !value)}>
              <Icon name="layers" size={13}/>{selectedNodeIds.length ? t('chat.selectedScope', { count: selectedNodeIds.length }) : t('chat.allDocuments')}
            </button>
            {scopeOpen && <div className="scope-popover">
              <div className="scope-popover-head">
                <strong>{t('chat.scope')}</strong>
                <button type="button" onClick={() => setScopeOpen(false)} aria-label={t('chat.scopeClose')}><Icon name="x" size={13}/></button>
              </div>
              <p>{selectedNodeIds.length ? t('chat.selectedScope', { count: selectedNodeIds.length }) : t('chat.scopeAllDescription')}</p>
              {scopeLoading && <small>{t('chat.scopeLoading')}</small>}
              {scopeFailed && <small className="scope-error">{t('chat.scopeFailed')}</small>}
              {!scopeLoading && !scopeFailed && <div className="chat-scope-list">{scopes.map((scope) => <label key={scope.uid} style={{ paddingLeft: `${scope.depth * 8}px` }}><input type="checkbox" checked={selectedNodeIds.includes(scope.uid)} onChange={() => toggleScope(scope.uid)}/><span><strong>{scope.name}</strong><small>{scope.nodeType === 'directory' ? t('chat.folderFiles', { count: scope.fileCount }) : scope.ext.toUpperCase()}</small></span></label>)}</div>}
              {selectedNodeIds.length > 0 && <button type="button" className="scope-clear" onClick={() => setSelectedNodeIds([])}>{t('chat.clearScope')}</button>}
            </div>}
          </div>
          {llmSettings && <div className="chat-compose-tool-group" ref={llmAnchorRef}>
            <button type="button" className={`compose-chip ${llmSettings.active.source === 'external' ? 'active' : ''}`} onClick={() => setLlmOpen((value) => !value)}>
              <Icon name="settings" size={13}/>{llmSettings.active.model || llmSettings.active.label}
            </button>
            {llmOpen && <div className="scope-popover">
              <div className="scope-popover-head">
                <strong>{t('chat.modelLabel')}</strong>
                <button type="button" onClick={() => setLlmOpen(false)} aria-label={t('chat.modelClose')}><Icon name="x" size={13}/></button>
              </div>
              {!llmSettings.can_edit && <p>{t('chat.modelReadOnly')}</p>}
              <div className="chat-scope-list">
                <label>
                  <input type="radio" name="llm-endpoint" checked={llmSettings.selected_endpoint_id === null} disabled={!llmSettings.can_edit || llmSaving} onChange={() => selectLlmEndpoint(null)}/>
                  <span><strong>{t('chat.modelServerDefault')}</strong><small>{llmSettings.server_default_model}</small></span>
                </label>
                {llmSettings.endpoints.map((endpoint) => <label key={endpoint.id}>
                  <input type="radio" name="llm-endpoint" checked={llmSettings.selected_endpoint_id === endpoint.id} disabled={!llmSettings.can_edit || llmSaving} onChange={() => selectLlmEndpoint(endpoint.id)}/>
                  <span><strong>{endpoint.name}</strong><small>{endpoint.default_model}</small></span>
                </label>)}
              </div>
              {llmError && <small className="scope-error">{t('chat.modelSelectFailed')}</small>}
            </div>}
          </div>}
        </div>
        <textarea
          ref={textareaRef}
          value={question}
          disabled={readOnlyConversation}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder={t('chat.placeholder')}
          rows={1}
        />
        <button className={`send-button ${busy ? 'is-stop' : ''}`} aria-label={busy ? t('chat.cancel') : t('chat.send')} disabled={readOnlyConversation || (!busy && !question.trim())}><Icon name={busy ? 'square' : 'arrow'} size={busy ? 15 : 17}/></button>
      </form>
    </section>
    <aside className="panel evidence-panel">
      <div className="evidence-head"><div><span className="eyebrow">{t('chat.evidenceLabel')}</span><h2>{t('chat.evidenceDocuments')}</h2></div><span>{citations.length}</span></div>
      <details className="chat-history" open>
        <summary><Icon name="clock" size={14}/>{t('chat.sessions')}</summary>
        <div className="chat-history-actions"><button type="button" className="scope-clear" onClick={() => onConversationChange(null)}>{t('chat.newSession')}</button>{activeConversation?.canManage && <button type="button" className="scope-clear" disabled={busy} onClick={openConversationManager}>{t('chat.manageSession')}</button>}</div>
        {historyLoading && <small>{t('chat.historyLoading')}</small>}
        {historyFailed && <small className="scope-error">{t('chat.historyFailed')}</small>}
        {!historyLoading && !historyFailed && conversations.length === 0 && <small>{t('chat.historyEmpty')}</small>}
        {conversations.length > 0 && <div className="chat-history-list">{conversations.map((item) => <button type="button" className={item.uid === conversationUid ? 'active' : ''} key={item.uid} onClick={() => onConversationChange(item.uid)}><strong>{item.title || t('chat.untitledSession')}</strong><span>{historyDate(item)}</span></button>)}</div>}
      </details>
      {citations.length === 0 ? <div className="evidence-empty"><Icon name="document" size={26}/><strong>{phase === 'done' ? t('chat.noEvidenceFound') : t('chat.noEvidence')}</strong><p>{phase === 'done' ? t('chat.noEvidenceFoundDescription') : t('chat.noEvidenceDescription')}</p></div> : <div className="evidence-list">{citations.map((citation) => <div id={`rag-citation-${citation.id}`} key={`${citation.nodeId}:${citation.id}`}><ResultCard result={citationResult(citation)} rank={citation.id} onOpen={() => onOpenDocument(citation.nodeId)}/></div>)}</div>}
    </aside>
    {manageOpen && activeConversation && <div className="modal-backdrop" onMouseDown={manageSaving ? undefined : () => setManageOpen(false)}><form className="compact-dialog conversation-dialog" onSubmit={(event) => void saveConversation(event)} onMouseDown={(event) => event.stopPropagation()}><div className="dialog-head"><div><h2>{t('chat.manageTitle')}</h2><p>{t('chat.manageDescription')}</p></div><button type="button" disabled={manageSaving} onClick={() => setManageOpen(false)}><Icon name="x" /></button></div><label className="dialog-field"><span>{t('chat.titleLabel')}</span><input autoFocus maxLength={160} value={manageTitle} disabled={manageSaving} onChange={(event) => setManageTitle(event.target.value)} /></label><fieldset className="conversation-scope-field" disabled={manageSaving}><legend>{t('chat.defaultScope')}</legend><p>{manageNodeIds.length ? t('chat.selectedScope', { count: manageNodeIds.length }) : t('chat.scopeAllDescription')}</p><div className="chat-scope-list">{scopes.map((scope) => <label key={scope.uid} style={{ paddingLeft: `${scope.depth * 8}px` }}><input type="checkbox" checked={manageNodeIds.includes(scope.uid)} onChange={() => toggleManageScope(scope.uid)}/><span><strong>{scope.name}</strong><small>{scope.nodeType === 'directory' ? t('chat.folderFiles', { count: scope.fileCount }) : scope.ext.toUpperCase()}</small></span></label>)}</div>{manageNodeIds.length > 0 && <button type="button" className="scope-clear" onClick={() => setManageNodeIds([])}>{t('chat.clearScope')}</button>}</fieldset>{manageError && <p className="dialog-error" role="alert">{manageError instanceof ApiClientError && manageError.code === 'REVISION_CONFLICT' ? t('chat.revisionConflict') : manageError instanceof ApiClientError && manageError.code === 'CONVERSATION_BUSY' ? t('chat.deleteBusy') : manageError.message}</p>}<div className="dialog-actions conversation-dialog-actions"><button type="button" className="secondary-button danger-text" disabled={manageSaving} onClick={() => setDeleteConfirm(true)}><Icon name="trash" size={13}/>{t('chat.deleteSession')}</button><span/><button type="button" className="secondary-button" disabled={manageSaving} onClick={() => setManageOpen(false)}>{t('chat.close')}</button><button type="submit" className="primary-button" disabled={manageSaving || !manageTitle.trim()}>{t('chat.saveSession')}</button></div></form></div>}
    {deleteConfirm && activeConversation && <div className="modal-backdrop conversation-confirm" onMouseDown={manageSaving ? undefined : () => setDeleteConfirm(false)}><div className="compact-dialog" onMouseDown={(event) => event.stopPropagation()}><div className="dialog-head"><div><h2>{t('chat.deleteTitle')}</h2><p>{t('chat.deleteDescription')}</p></div><button type="button" disabled={manageSaving} onClick={() => setDeleteConfirm(false)}><Icon name="x" /></button></div>{manageError && <p className="dialog-error" role="alert">{manageError instanceof ApiClientError && manageError.code === 'CONVERSATION_BUSY' ? t('chat.deleteBusy') : manageError.message}</p>}<div className="dialog-actions"><button type="button" className="secondary-button" disabled={manageSaving} onClick={() => setDeleteConfirm(false)}>{t('chat.close')}</button><button type="button" className="primary-button danger" disabled={manageSaving} onClick={() => void deleteConversation()}>{t('chat.deleteConfirm')}</button></div></div></div>}
  </div>
}
