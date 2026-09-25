/** What an upload tells the server so it can say how the model will be listed:
 * the small text files that describe it and each weight shard's header. The
 * weights themselves never leave the browser for this. */

interface PreviewEntry {
  path: string
  file: Blob
}

export interface ListingPreviewRequest {
  repo_id: string
  paths: string[]
  config: string | null
  quant_config: string | null
  readme: string | null
  headers: Record<string, string>
}

// The same ceilings the server applies (backend/app/listing.py).
const MAX_TEXT_BYTES = 1_000_000
const MAX_HEADER_BYTES = 16_000_000
const MAX_SHARDS = 1_000

async function text(entry: PreviewEntry | undefined): Promise<string | null> {
  if (!entry || entry.file.size > MAX_TEXT_BYTES) return null
  try {
    return await entry.file.text()
  } catch {
    return null
  }
}

/** A SafeTensors header: an 8-byte little-endian length, then that much JSON. */
export async function safetensorsHeader(file: Blob): Promise<string | null> {
  if (file.size < 8) return null
  try {
    const view = new DataView(await file.slice(0, 8).arrayBuffer())
    const length = Number(view.getBigUint64(0, true))
    if (length <= 0 || length > MAX_HEADER_BYTES || 8 + length > file.size) return null
    return await file.slice(8, 8 + length).text()
  } catch {
    return null
  }
}

export async function listingPreviewRequest(repoId: string, entries: PreviewEntry[]): Promise<ListingPreviewRequest> {
  const top = (name: string) => entries.find((entry) => entry.path === name)
  const shards = entries.filter((entry) => entry.path.toLowerCase().endsWith('.safetensors')).slice(0, MAX_SHARDS)
  const headers: Record<string, string> = {}
  // A few at a time: checkpoints can have hundreds of shards.
  for (let index = 0; index < shards.length; index += 8) {
    const batch = shards.slice(index, index + 8)
    const read = await Promise.all(batch.map((entry) => safetensorsHeader(entry.file)))
    batch.forEach((entry, position) => {
      const header = read[position]
      if (header != null) headers[entry.path] = header
    })
  }
  const [config, quant, readme] = await Promise.all([
    text(top('config.json')),
    text(top('hf_quant_config.json')),
    text(top('README.md')),
  ])
  return {
    repo_id: repoId,
    paths: entries.map((entry) => entry.path),
    config,
    quant_config: quant,
    readme,
    headers,
  }
}
