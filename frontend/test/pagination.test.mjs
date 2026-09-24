import assert from 'node:assert/strict'
import test from 'node:test'

import { pageList } from '../src/pagination.ts'

test('short page runs are listed in full', () => {
  assert.deepEqual(pageList(1, 1), [1])
  assert.deepEqual(pageList(2, 5), [1, 2, 3, 4, 5])
})

test('long page runs keep the ends and the neighbours of the current page', () => {
  assert.deepEqual(pageList(1, 20), [1, 2, 3, 4, null, 20])
  assert.deepEqual(pageList(10, 20), [1, null, 9, 10, 11, null, 20])
  assert.deepEqual(pageList(20, 20), [1, null, 17, 18, 19, 20])
})

test('a gap of one page shows that page instead of an ellipsis', () => {
  assert.deepEqual(pageList(4, 20), [1, 2, 3, 4, 5, null, 20])
  assert.deepEqual(pageList(5, 7), [1, 2, 3, 4, 5, 6, 7])
})
