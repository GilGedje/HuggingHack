/**
 * Placeholders shaped like the content on its way, so the page keeps its layout
 * while it loads. They fade in after a short delay (`.skeleton-reveal`), so a
 * fast load never flashes them.
 */

function Bar({ width, height = 9, round = false }: { width: number | string; height?: number; round?: boolean }) {
  return <span className={round ? 'skeleton skeleton-round' : 'skeleton'} style={{ width, height }} />
}

/** Model cards as the library grid shows them: task strip, monogram, and body. */
export function ModelCardSkeletons({ count = 8, label = 'Loading models' }: { count?: number; label?: string }) {
  return (
    <div className="model-card-grid skeleton-card-grid skeleton-reveal" aria-busy="true" aria-label={label}>
      {Array.from({ length: count }).map((_, index) => (
        <div key={index} className="model-card skeleton-card" aria-hidden="true">
          <div className="model-visual skeleton-visual-frame">
            <div className="model-visual-topline">
              <Bar width={96} />
              <Bar width={58} />
            </div>
            <div className="model-visual-core">
              <span className="skeleton skeleton-round skeleton-monogram" />
              <div className="parameter-viz">
                {Array.from({ length: 6 }).map((_, bar) => <span key={bar} className="skeleton" />)}
              </div>
            </div>
            <div className="model-visual-caption">
              <Bar width={130} />
            </div>
          </div>
          <div className="model-card-body">
            <Bar width="34%" />
            <Bar width="72%" height={15} />
            <div className="skeleton-pills">
              <Bar width={44} height={18} />
              <Bar width={78} height={18} />
              <Bar width={64} height={18} />
            </div>
            <Bar width="62%" />
            <div className="skeleton-actions">
              <Bar width={104} height={32} />
              <Bar width={34} height={34} />
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

/** Rows of a list or table: a name with a detail line, and a few short cells. */
export function RowSkeletons({ rows = 5, cells = 3, label = 'Loading' }: { rows?: number; cells?: number; label?: string }) {
  return (
    <div className="skeleton-rows skeleton-reveal" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="skeleton-row" aria-hidden="true">
          <span className="skeleton skeleton-round skeleton-avatar" />
          <div className="skeleton-row-text">
            <Bar width={`${48 + ((index * 17) % 30)}%`} height={12} />
            <Bar width={`${26 + ((index * 11) % 22)}%`} />
          </div>
          {Array.from({ length: cells }).map((_, cell) => <Bar key={cell} width={56 + cell * 10} />)}
        </div>
      ))}
    </div>
  )
}

/** A model page: title block, tabs, the card, and the side facts. */
export function ModelPageSkeleton() {
  return (
    <div className="model-page skeleton-reveal" aria-busy="true" aria-label="Loading the model">
      <div className="model-hero" aria-hidden="true">
        <div className="model-hero-inner skeleton-hero">
          <div className="skeleton-hero-title">
            <span className="skeleton skeleton-round skeleton-avatar" />
            <Bar width={320} height={24} />
          </div>
          <div className="skeleton-pills">
            <Bar width={96} height={20} />
            <Bar width={70} height={20} />
            <Bar width={84} height={20} />
          </div>
          <div className="skeleton-tabs">
            {[88, 120, 72, 60, 64].map((width) => <Bar key={width} width={width} height={12} />)}
          </div>
        </div>
      </div>
      <div className="skeleton-model-body" aria-hidden="true">
        <div className="skeleton-document">
          <Bar width="46%" height={20} />
          {[92, 86, 95, 70, 88, 60].map((width, index) => <Bar key={index} width={`${width}%`} height={11} />)}
          <Bar width="38%" height={16} />
          {[90, 82, 76].map((width, index) => <Bar key={index} width={`${width}%`} height={11} />)}
        </div>
        <div className="skeleton-aside">
          {[70, 54, 62, 48, 58].map((width, index) => (
            <div key={index} className="skeleton-fact">
              <Bar width={60} />
              <Bar width={`${width}%`} />
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

/** The Storage page: the summary strip and one location with its models. */
export function StorageSkeleton() {
  return (
    <div className="skeleton-reveal" aria-busy="true" aria-label="Checking storage">
      <section className="storage-strip skeleton-strip" aria-hidden="true">
        <span className="skeleton skeleton-icon" />
        <div className="storage-main">
          <Bar width="42%" height={13} />
          <Bar width="100%" height={5} />
          <Bar width="30%" />
        </div>
      </section>
      <section className="storage-target skeleton-target" aria-hidden="true">
        <div className="skeleton-hero-title">
          <span className="skeleton skeleton-icon" />
          <div className="skeleton-row-text">
            <Bar width={180} height={14} />
            <Bar width={240} />
          </div>
        </div>
        <RowSkeletons rows={4} cells={4} label="Loading models" />
      </section>
    </div>
  )
}
