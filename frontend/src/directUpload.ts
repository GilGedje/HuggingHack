import type { DirectPartLink, DirectUploadState } from './types'

/**
 * Uploads that go from the browser straight to the bucket: HuggingHack starts a
 * multipart upload and signs a link per part; the browser PUTs each part to its
 * link, several at once, and asks HuggingHack to complete the file. What reached
 * the bucket always comes back from the server, so a reload carries on from there.
 */

/** Parts in flight at once per file. */
export const PARALLEL_PARTS = 6
/** Signed links asked for per request. */
export const LINKS_PER_REQUEST = 24
/** Tries per part before the upload stops with an error. */
export const PART_ATTEMPTS = 3

export interface DirectEndpoints {
  parts: (numbers: number[]) => Promise<{ parts: DirectPartLink[] }>
  complete: () => Promise<DirectUploadState>
}

export type PutPart = (url: string, body: Blob, signal?: AbortSignal) => Promise<Response>

/** Byte range [start, end) of a part, numbered from 1. */
export function partRange(state: Pick<DirectUploadState, 'size' | 'part_size'>, number: number): [number, number] {
  const start = (number - 1) * state.part_size
  return [start, Math.min(state.size, start + state.part_size)]
}

/** Parts the bucket does not have yet, in order. */
export function pendingParts(state: Pick<DirectUploadState, 'part_count' | 'done' | 'complete'>): number[] {
  if (state.complete) return []
  const done = new Set(state.done)
  const pending: number[] = []
  for (let number = 1; number <= state.part_count; number += 1) {
    if (!done.has(number)) pending.push(number)
  }
  return pending
}

/** Bytes the bucket has acknowledged, counting whole parts. */
export function acknowledgedBytes(state: Pick<DirectUploadState, 'size' | 'part_size'>, done: Iterable<number>): number {
  let total = 0
  for (const number of done) {
    const [start, end] = partRange(state, number)
    total += end - start
  }
  return total
}

/** The storage's address as people know it: host and port, from a signed link. */
export function storageHost(url: string): string {
  const match = /^[a-z]+:\/\/([^/?#]+)/i.exec(url)
  return match ? match[1] : 'the storage'
}

export function storageUnreachableMessage(url: string): string {
  return (
    `The browser could not reach the storage at ${storageHost(url)}. ` +
    'If it uses a private certificate authority, this computer must trust it, ' +
    'and the bucket must allow uploads from this site (CORS).'
  )
}

export function storageRefusedMessage(status: number, url: string): string {
  return `The storage at ${storageHost(url)} refused part of the file (${status}). Retry to continue the upload.`
}

const defaultPut: PutPart = (url, body, signal) =>
  // No cookies and no HuggingHack headers: the signature in the link is the permission.
  fetch(url, { method: 'PUT', body, signal, credentials: 'omit' })

export async function uploadDirect(
  file: Blob,
  state: DirectUploadState,
  endpoints: DirectEndpoints,
  onProgress: (uploaded: number) => void,
  signal?: AbortSignal,
  put: PutPart = defaultPut,
): Promise<void> {
  const done = new Set(state.done)
  onProgress(acknowledgedBytes(state, done))
  const pending = pendingParts(state)
  for (let index = 0; index < pending.length; index += LINKS_PER_REQUEST) {
    const batch = pending.slice(index, index + LINKS_PER_REQUEST)
    const links = new Map((await endpoints.parts(batch)).parts.map((link) => [link.number, link]))
    let next = 0
    const worker = async () => {
      while (next < batch.length) {
        const number = batch[next]
        next += 1
        await sendPart(file, state, number, links, endpoints, put, signal)
        done.add(number)
        onProgress(acknowledgedBytes(state, done))
      }
    }
    await Promise.all(Array.from({ length: Math.min(PARALLEL_PARTS, batch.length) }, worker))
  }
  const finished = await endpoints.complete()
  onProgress(finished.complete ? state.size : acknowledgedBytes(state, finished.done))
}

async function sendPart(
  file: Blob,
  state: DirectUploadState,
  number: number,
  links: Map<number, DirectPartLink>,
  endpoints: DirectEndpoints,
  put: PutPart,
  signal?: AbortSignal,
): Promise<void> {
  const [start, end] = partRange(state, number)
  let failure = ''
  for (let attempt = 1; attempt <= PART_ATTEMPTS; attempt += 1) {
    let link = links.get(number)
    if (!link) {
      link = (await endpoints.parts([number])).parts[0]
      links.set(number, link)
    }
    let response: Response
    try {
      response = await put(link.url, file.slice(start, end), signal)
    } catch (reason) {
      if (signal?.aborted || (reason instanceof DOMException && reason.name === 'AbortError')) throw reason
      failure = storageUnreachableMessage(link.url)
      continue
    }
    if (response.ok) return
    failure = storageRefusedMessage(response.status, link.url)
    // An expired or refused link is signed again for the next try.
    links.delete(number)
  }
  throw new Error(failure)
}
