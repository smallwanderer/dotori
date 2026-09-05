import type { SearchResult } from '../api/models'
import { useI18n } from '../i18n'

export function ResultCard({ result, rank }: { result: SearchResult; rank: number }) {
  const { t } = useI18n()
  const location = result.section && result.page
    ? t('search.sectionPage', { section: result.section, page: result.page })
    : result.section
      ? t('search.sectionOnly', { section: result.section })
      : result.page
        ? t('search.pageOnly', { page: result.page })
        : result.fileType
  return <article className="result-card">
    <div className="result-rank">{rank}</div>
    <div className="result-body">
      <div><strong>{result.title}</strong><span>{result.score.toFixed(2)}</span></div>
      <p>{result.text}</p>
      <small>{location} · {t('search.evidenceCount', { count: result.evidences.length })}</small>
    </div>
  </article>
}
