import { type FormEvent, useEffect, useRef, useState } from 'react'
import { ApiClientError } from '../../api/http'
import type { ChatPhase, RagCitation, RagHistoryItem, RagStarted, SearchResult, SearchScopeNode } from '../../api/models'
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

export function ChatFeature() {
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
  const [history, setHistory] = useState<RagHistoryItem[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyFailed, setHistoryFailed] = useState(false)
  const cancelRef = useRef<() => void>(() => undefined)
  const busy = phase === 'searching' || phase === 'preparing' || phase === 'streaming'

  useEffect(() => {
    let active = true
    workspaceApi.listSearchScopes()
      .then((nodes) => { if (active) setScopes(nodes) })
      .catch(() => { if (active) setScopeFailed(true) })
      .finally(() => { if (active) setScopeLoading(false) })
    workspaceApi.listRagHistory()
      .then((items) => { if (active) setHistory(items) })
      .catch(() => { if (active) setHistoryFailed(true) })
      .finally(() => { if (active) setHistoryLoading(false) })
    return () => {
      active = false
      cancelRef.current()
    }
  }, [])

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
    return current.message
  }

  function startRequest() {
    const nextQuestion = question.trim()
    if (!nextQuestion) return
    cancelRef.current()
    setSubmittedQuestion(nextQuestion)
    setAnswer('')
    setCitations([])
    setError(null)
    setStarted(null)
    setMetrics({})
    cancelRef.current = workspaceApi.streamAnswer({
      question: nextQuestion,
      language: locale,
      nodeIds: selectedNodeIds,
    }, {
      onPhase: setPhase,
      onStarted: setStarted,
      onSources: setCitations,
      onToken: (token) => setAnswer((current) => current + token),
      onCompleted: (completed) => {
        setAnswer(completed.answer)
        setCitations(completed.citations)
        setMetrics(completed.performanceMetrics)
        workspaceApi.listRagHistory().then(setHistory).catch(() => setHistoryFailed(true))
      },
      onCanceled: setMetrics,
      onError: setError,
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
    cancelRef.current()
    cancelRef.current = () => undefined
    setPhase('canceled')
  }

  function toggleScope(uid: string) {
    setSelectedNodeIds((current) => current.includes(uid)
      ? current.filter((value) => value !== uid)
      : [...current, uid])
  }

  function scrollToCitation(id: number) {
    document.getElementById(`rag-citation-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  function loadHistory(item: RagHistoryItem) {
    cancelRef.current()
    setQuestion(item.question)
    setSubmittedQuestion(item.question)
    setAnswer(item.answer)
    setCitations(item.citations)
    setError(null)
    setStarted({ jobId: item.id, llmTarget: '', llmModel: item.llmModel })
    setMetrics(item.performanceMetrics)
    setPhase('done')
  }

  function historyDate(item: RagHistoryItem): string {
    const date = new Date(item.completedAt ?? item.createdAt)
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
  }

  const duration = durationMetric(metrics)

  return <div className="chat-layout">
    <section className="panel chat-panel">
      <div className="chat-header">
        <div><span className="ai-orb"><Icon name="sparkles" size={17}/></span><span><strong>{t('chat.assistant')}</strong><small>{t('chat.liveApi')}</small></span></div>
      </div>
      <div className="chat-scroll">
        {phase === 'idle' && <div className="chat-welcome"><span className="ai-orb large"><Icon name="sparkles" size={23}/></span><h2>{t('chat.askTitle')}</h2><p>{t('chat.askDescription')}</p><div className="suggestions"><button onClick={() => setQuestion(t('chat.suggestionPolicy'))}>{t('chat.suggestionPolicy')}</button><button onClick={() => setQuestion(t('chat.suggestionStrategy'))}>{t('chat.suggestionStrategy')}</button></div></div>}
        {phase !== 'idle' && <>
          <div className="message user-message">{submittedQuestion}</div>
          <div className={`message assistant-message ${phase === 'error' ? 'has-error' : ''}`}>
            <div className="answer-status"><span className={busy ? 'pulse-dot' : 'complete-dot'}>{phase === 'done' && <Icon name="check" size={11}/>}</span>{statusText}</div>
            {answer && <p className="answer-text">{answer}</p>}
            {error && <div className="chat-error"><p>{errorMessage(error)}</p><button type="button" onClick={startRequest}><Icon name="refresh" size={13}/>{t('chat.retry')}</button></div>}
            {phase === 'canceled' && <button type="button" className="chat-retry" onClick={startRequest}><Icon name="refresh" size={13}/>{t('chat.retry')}</button>}
            {phase === 'done' && citations.length > 0 && <div className="citation-row">{citations.map((citation) => <button type="button" key={citation.id} onClick={() => scrollToCitation(citation.id)}>[{citation.id}] {citation.nodeName}</button>)}</div>}
            {phase === 'done' && <div className="chat-meta">{started?.llmModel && <span>{t('chat.model', { model: started.llmModel })}</span>}{duration !== null && <span>{t('chat.duration', { duration })}</span>}</div>}
          </div>
        </>}
      </div>
      <form className="chat-compose" onSubmit={submit}>
        <textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder={t('chat.placeholder')}/>
        <div><span><Icon name="layers" size={15}/>{selectedNodeIds.length ? t('chat.selectedScope', { count: selectedNodeIds.length }) : t('chat.allDocuments')}</span><button className={busy ? 'is-stop' : ''} aria-label={busy ? t('chat.cancel') : t('chat.send')} disabled={!busy && !question.trim()}><Icon name={busy ? 'square' : 'arrow'} size={busy ? 15 : 17}/></button></div>
      </form>
    </section>
    <aside className="panel evidence-panel">
      <div className="evidence-head"><div><span className="eyebrow">{t('chat.evidenceLabel')}</span><h2>{t('chat.evidenceDocuments')}</h2></div><span>{citations.length}</span></div>
      <details className="chat-history">
        <summary><Icon name="clock" size={14}/>{t('chat.history')}</summary>
        {historyLoading && <small>{t('chat.historyLoading')}</small>}
        {historyFailed && <small className="scope-error">{t('chat.historyFailed')}</small>}
        {!historyLoading && !historyFailed && history.length === 0 && <small>{t('chat.historyEmpty')}</small>}
        {history.length > 0 && <div className="chat-history-list">{history.map((item) => <button type="button" key={item.id} onClick={() => loadHistory(item)}><strong>{item.question}</strong><span>{historyDate(item)}</span><small>{item.answerPreview}</small></button>)}</div>}
      </details>
      <details className="chat-scope">
        <summary><Icon name="layers" size={14}/>{t('chat.scope')}</summary>
        <p>{selectedNodeIds.length ? t('chat.selectedScope', { count: selectedNodeIds.length }) : t('chat.scopeAllDescription')}</p>
        {scopeLoading && <small>{t('chat.scopeLoading')}</small>}
        {scopeFailed && <small className="scope-error">{t('chat.scopeFailed')}</small>}
        {!scopeLoading && !scopeFailed && <div className="chat-scope-list">{scopes.map((scope) => <label key={scope.uid} style={{ paddingLeft: `${scope.depth * 8}px` }}><input type="checkbox" checked={selectedNodeIds.includes(scope.uid)} onChange={() => toggleScope(scope.uid)}/><span><strong>{scope.name}</strong><small>{scope.nodeType === 'directory' ? t('chat.folderFiles', { count: scope.fileCount }) : scope.ext.toUpperCase()}</small></span></label>)}</div>}
        {selectedNodeIds.length > 0 && <button type="button" className="scope-clear" onClick={() => setSelectedNodeIds([])}>{t('chat.clearScope')}</button>}
      </details>
      {citations.length === 0 ? <div className="evidence-empty"><Icon name="document" size={26}/><strong>{phase === 'done' ? t('chat.noEvidenceFound') : t('chat.noEvidence')}</strong><p>{phase === 'done' ? t('chat.noEvidenceFoundDescription') : t('chat.noEvidenceDescription')}</p></div> : <div className="evidence-list">{citations.map((citation) => <div id={`rag-citation-${citation.id}`} key={`${citation.nodeId}:${citation.id}`}><ResultCard result={citationResult(citation)} rank={citation.id}/></div>)}</div>}
    </aside>
  </div>
}
