import assert from 'node:assert/strict'
import test from 'node:test'

import { LEASE_TTL_MS, claimOrphans, parseTabRecord } from '../src/uploadStore.ts'

const now = 1_000_000

test('a tab only takes over the jobs of tabs that are gone', () => {
  const records = [
    ['uploads:live', { seen: now - 1000, jobs: [{ id: 'running' }] }],
    ['uploads:stale', { seen: now - LEASE_TTL_MS - 1, jobs: [{ id: 'crashed' }] }],
    ['uploads:closed', { seen: 0, jobs: [{ id: 'closed' }, { id: 'crashed' }] }],
    ['uploads:self', { seen: 0, jobs: [{ id: 'own' }] }],
  ]
  assert.deepEqual(claimOrphans(records, 'uploads:self', now), {
    jobs: [{ id: 'crashed' }, { id: 'closed' }],
    keys: ['uploads:stale', 'uploads:closed'],
  })
})

test('claiming reads without changing anything, so it can run twice', () => {
  const records = [['uploads:gone', { seen: 0, jobs: [{ id: 'one' }] }]]
  const first = claimOrphans(records, 'uploads:self', now)
  assert.deepEqual(claimOrphans(records, 'uploads:self', now), first)
  assert.deepEqual(records[0][1].jobs, [{ id: 'one' }])
})

test('stored records are read in the current and the old shared format', () => {
  assert.deepEqual(parseTabRecord('{"seen":5,"jobs":[{"id":"a"}]}'), { seen: 5, jobs: [{ id: 'a' }] })
  // The old single list belonged to no tab, so it is always taken over.
  assert.deepEqual(parseTabRecord('[{"id":"a"}]'), { seen: 0, jobs: [{ id: 'a' }] })
  assert.equal(parseTabRecord('{broken'), null)
  assert.equal(parseTabRecord('{"seen":1}'), null)
  assert.equal(parseTabRecord(null), null)
})
