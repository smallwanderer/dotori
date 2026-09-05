import { useEffect, useState, type CSSProperties } from 'react'
import type { SearchMode, SearchQueryPlan, SearchResult, SearchScopeNode } from '../../api/models'
import { ApiClientError } from '../../api/http'
import { workspaceApi } from '../../api/workspace'
import { AsyncState, type AsyncStateKind } from '../../components/AsyncState'
import { Icon } from '../../components/Icon'
import { ResultCard } from '../../components/ResultCard'
import { useI18n } from '../../i18n'

type SearchState = AsyncStateKind | 'ready'

function filterLabel(filter: Record<string, unknown>): string {
  if (typeof filter.source_text === 'string' && filter.source_text) return filter.source_text
  return [filter.field, filter.operator, filter.value]
    .filter((value) => value !== undefined && value !== '')
    .map(String)
    .join(' ')
}

export function SearchFeature() {
  const { t } = useI18n()
  const [mode, setMode] = useState<SearchMode>('basic')
  const [query, setQuery] = useState('')
  const [scopes, setScopes] = useState<SearchScopeNode[]>([])
  const [selectedNodeIds, setSelectedNodeIds] = useState<string[]>([])
  const [scopeLoading, setScopeLoading] = useState(true)
  const [scopeError, setScopeError] = useState(false)
  const [results, setResults] = useState<SearchResult[]>([])
  const [queryPlan, setQueryPlan] = useState<SearchQueryPlan | null>(null)
  const [durationMs, setDurationMs] = useState<number | null>(null)
  const [errorDetail, setErrorDetail] = useState('')
  const [state, setState] = useState<SearchState>('empty')

  useEffect(() => {
    let active = true
    workspaceApi.listSearchScopes()
      .then((nodes) => {
        if (!active) return
        setScopes(nodes)
        setScopeError(false)
      })
      .catch(() => {
        if (active) setScopeError(true)
      })
      .finally(() => {
        if (active) setScopeLoading(false)
      })
    return () => { active = false }
  }, [])

  function selectMode(nextMode: SearchMode) {
    setMode(nextMode)
    setQueryPlan(null)
    setErrorDetail('')
  }

  function toggleScope(uid: string) {
    setSelectedNodeIds((current) => current.includes(uid)
      ? current.filter((item) => item !== uid)
      : [...current, uid])
  }

  async function runSearch() {
    const normalizedQuery = query.trim()
    if (!normalizedQuery) {
      setErrorDetail(t('search.queryRequired'))
      return
    }
    setErrorDetail('')
    setState('loading')
    try {
      const response = await workspaceApi.searchDocuments({
        mode,
        query: normalizedQuery,
        nodeIds: selectedNodeIds,
      })
      setResults(response.results)
      setQueryPlan(response.queryPlan)
      setDurationMs(response.durationMs)
      setState(response.results.length ? 'ready' : 'empty')
    } catch (error) {
      setErrorDetail(error instanceof Error ? error.message : t('state.errorDescription'))
      setState(error instanceof ApiClientError && error.unauthorized ? 'unauthorized' : 'error')
    }
  }

  return <div className="search-page">
    <div className="search-page-header"><div><span className="eyebrow">{t('search.workspace')}</span><h1>{t('search.documentSearch')}</h1><p>{mode === 'basic' ? t('search.basicDescription') : t('search.advancedDescription')}</p></div><div className="search-mode-switch" role="tablist" aria-label={t('search.mode')}><button role="tab" aria-selected={mode === 'basic'} className={mode === 'basic' ? 'active' : ''} onClick={() => selectMode('basic')}>{t('search.basic')}</button><button role="tab" aria-selected={mode === 'advanced'} className={mode === 'advanced' ? 'active' : ''} onClick={() => selectMode('advanced')}>{t('search.advanced')}</button></div></div>
    <div className={`split-layout ${mode === 'basic' ? 'basic-search' : ''}`}>
      <form className="panel retrieval-config" onSubmit={(event) => { event.preventDefault(); void runSearch() }}><div className="panel-title"><div><span className="eyebrow">{mode === 'basic' ? t('search.directPipeline') : t('search.understandingPipeline')}</span><h2>{t('search.settings')}</h2></div><span className="draft-pill live">{t('search.liveApi')}</span></div><label className="field-label" htmlFor="search-query">{mode === 'basic' ? t('search.keywords') : t('search.query')}</label><textarea id="search-query" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={mode === 'basic' ? t('search.basicPlaceholder') : t('search.advancedPlaceholder')} />{errorDetail && state !== 'error' && state !== 'unauthorized' && <p className="field-error" role="alert">{errorDetail}</p>}<div className="pipeline-note"><strong>{t('search.profileManaged')}</strong><small>{t('search.profileManagedDescription')}</small></div><fieldset className="scope-picker"><legend>{t('search.scope')}</legend><p>{selectedNodeIds.length ? t('search.scopeSelected', { count: selectedNodeIds.length }) : t('search.scopeAllDescription')}</p>{scopeLoading && <small>{t('search.scopeLoading')}</small>}{scopeError && <small className="field-error">{t('search.scopeFailed')}</small>}{!scopeLoading && !scopeError && !scopes.length && <small>{t('search.noScopes')}</small>}<div className="scope-node-list">{scopes.map((node) => <label key={node.uid} style={{ '--scope-depth': node.depth } as CSSProperties}><input type="checkbox" checked={selectedNodeIds.includes(node.uid)} onChange={() => toggleScope(node.uid)} /><span><strong>{node.name}</strong><small>{node.nodeType === 'directory' ? t('search.folderFiles', { count: node.fileCount }) : node.ext.replace('.', '').toUpperCase()}</small></span></label>)}</div>{!!selectedNodeIds.length && <button type="button" className="text-button" onClick={() => setSelectedNodeIds([])}>{t('search.clearScope')}</button>}</fieldset>{mode === 'advanced' && <div className="strategy-box"><div><Icon name="layers"/><span><strong>{t('search.advancedPipeline')}</strong><small>{t('search.advancedPipelineDescription')}</small></span></div></div>}<button className="primary-button wide" type="submit" disabled={state === 'loading'}><Icon name="search" size={17}/>{state === 'loading' ? t('search.running') : t('search.run')}</button></form>
      <section className="panel retrieval-results"><div className="results-header"><div><span className="eyebrow">{t('search.topResults')}</span><h2>{t('search.results')}</h2></div>{durationMs !== null && <span>{t('search.duration', { duration: durationMs.toFixed(1) })}</span>}</div>{mode === 'advanced' && queryPlan && <div className="query-plan"><Icon name="sparkles" size={15}/><div><strong>{t('search.interpretedQuery')}</strong><p>{queryPlan.retrievalQuery}</p><small>{t('search.planMeta', { source: queryPlan.source, confidence: queryPlan.confidence === null ? '—' : `${Math.round(queryPlan.confidence * 100)}%` })}</small>{!!queryPlan.filters.length && <small>{t('search.planFilters')}: {queryPlan.filters.map(filterLabel).join(' · ')}</small>}{queryPlan.warnings.map((warning) => <small className="plan-warning" key={`${warning.code}-${warning.message}`}>{warning.message}</small>)}</div></div>}{(state === 'error' || state === 'unauthorized') && errorDetail && <p className="search-error-detail" role="alert">{errorDetail}</p>}{state === 'ready' ? <div className="result-list">{results.map((result, index) => <ResultCard key={result.uid} result={result} rank={index + 1} />)}</div> : <AsyncState kind={state} onRetry={state === 'error' ? () => void runSearch() : undefined} />}</section>
    </div>
  </div>
}
