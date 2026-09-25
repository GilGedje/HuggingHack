import { ArrowUpRight, Building2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { precisionLabel } from '../catalog'
import { RELATIONS, relationGroup, relationName } from '../modelTree'
import type { LibraryModelDetails } from '../types'
import { formatNumber } from '../utils'

/**
 * Where a model comes from and what was made from it: its base model above it,
 * quantizations, fine-tunes, adapters, and merges below. Shown only when the model
 * has any of them.
 */
export function ModelTreeCard({ model }: { model: LibraryModelDetails }) {
  const { base, children } = model.model_tree
  const groups = RELATIONS.filter((relation) => children[relation]?.length)
  if (!base && !groups.length) return null
  return (
    <section className="aside-card model-tree" aria-label="Model tree">
      <h2>Model tree</h2>
      <ol className="model-tree-list">
        {base && (
          <li className="model-tree-node">
            <span className="model-tree-kind">Base model</span>
            {base.in_library ? (
              <Link to={`/models/${base.id}`} className="model-tree-name">{base.id}</Link>
            ) : (
              <span className="model-tree-name external" title="Not in this library">{base.id}</span>
            )}
            {!base.in_library && (
              <small>
                Not in this library
                {base.organization && (
                  <>
                    {' · '}
                    <Link to={`/orgs/${base.organization.name}`}>
                      <Building2 size={11} /> {base.organization.display_name}
                    </Link>
                  </>
                )}
              </small>
            )}
          </li>
        )}
        <li className="model-tree-node current" aria-current="page">
          <span className="model-tree-kind">{base ? relationName(base.relation) : 'This model'}</span>
          <strong className="model-tree-name">{model.id}</strong>
        </li>
        {groups.map((relation) => {
          const items = children[relation] || []
          return (
            <li key={relation} className="model-tree-node group">
              <Link
                className="model-tree-kind model-tree-group-link"
                to={`/models?base_model=${encodeURIComponent(model.id)}&relation=${relation}`}
                title={`See every ${relationGroup(relation, 1).toLowerCase()} of ${model.id} in Explore`}
              >
                {relationGroup(relation, items.length)} <em>{items.length}</em>
                <ArrowUpRight size={11} aria-hidden="true" />
              </Link>
              <ul>
                {items.map((child) => (
                  <li key={child.id}>
                    <Link to={`/models/${child.id}`} className="model-tree-name">{child.id}</Link>
                    <small>
                      {[precisionLabel(child.precision), child.parameter_count ? `${formatNumber(child.parameter_count)} params` : null]
                        .filter(Boolean)
                        .join(' · ')}
                    </small>
                  </li>
                ))}
              </ul>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
