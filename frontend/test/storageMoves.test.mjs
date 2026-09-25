import assert from 'node:assert/strict'
import test from 'node:test'
import { moveCancellable, movePercent, moveStep, moveUnfinished } from '../src/storageMoves.ts'

const move = (status, extra = {}) => ({
  status, total_bytes: 100, copied_bytes: 0, verified_bytes: 0, active_reads: 0, ...extra,
})

test('progress runs through copying and checking, then holds while downloads finish', () => {
  assert.equal(movePercent(move('queued')), 0)
  assert.equal(movePercent(move('copying', { copied_bytes: 50 })), 23)
  assert.equal(movePercent(move('verifying', { copied_bytes: 100, verified_bytes: 100 })), 92)
  assert.ok(movePercent(move('draining')) < movePercent(move('cleaning')))
  assert.equal(movePercent(move('done')), 100)
  assert.equal(movePercent(move('copying', { total_bytes: 0 })), 46)
})

test('steps, cancelling, and whether a move is still running', () => {
  assert.deepEqual(['queued', 'copying', 'verifying', 'switching', 'draining', 'cleaning', 'done', 'failed'].map((status) => moveStep(move(status))), [0, 0, 1, 2, 3, 3, 4, -1])
  assert.deepEqual(['queued', 'copying', 'verifying', 'switching', 'draining'].map((status) => moveCancellable({ status })), [true, true, true, false, false])
  assert.equal(moveUnfinished({ status: 'draining' }), true)
  assert.equal(moveUnfinished({ status: 'cancelled' }), false)
})
