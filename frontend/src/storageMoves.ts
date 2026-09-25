/** Reading a storage move's state for the Storage page. */

interface MoveState {
  status: string
  total_bytes: number
  copied_bytes: number
  verified_bytes: number
  active_reads: number
}

export const MOVE_STEPS = ['Copy', 'Verify', 'Switch', 'Clean up'] as const

const STEP_OF: Record<string, number> = {
  queued: 0, copying: 0, verifying: 1, switching: 2, draining: 3, cleaning: 3, done: 4,
}

export function moveUnfinished(move: { status: string }): boolean {
  return !['done', 'failed', 'cancelled'].includes(move.status)
}

/** Which of MOVE_STEPS the move is on; 4 once it is done, -1 when it stopped. */
export function moveStep(move: { status: string }): number {
  return STEP_OF[move.status] ?? -1
}

/** How far along the whole move is, 0-100. Copying and checking are most of it;
 * waiting for downloads of the old copy has no known length, so it holds still. */
export function movePercent(move: MoveState): number {
  const share = (part: number) => (move.total_bytes ? Math.min(1, part / move.total_bytes) : 1)
  switch (move.status) {
    case 'queued':
      return 0
    case 'copying':
      return 46 * share(move.copied_bytes)
    case 'verifying':
      return 46 + 46 * share(move.verified_bytes)
    case 'switching':
      return 93
    case 'draining':
      return 95
    case 'cleaning':
      return 98
    case 'done':
      return 100
    default:
      return 0
  }
}

/** Only a move that has not switched yet can be called off. */
export function moveCancellable(move: { status: string }): boolean {
  return ['queued', 'copying', 'verifying'].includes(move.status)
}
