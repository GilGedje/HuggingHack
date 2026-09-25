import assert from 'node:assert/strict'
import test from 'node:test'
import { bestOf, compareRows, formatValue, headlineMetrics } from '../src/configCompare.ts'

const metrics = [
  { id: 'output_tps', label: 'Output throughput', unit: 'tok/s', better: 'higher', group: 'speed' },
  { id: 'ttft_ms', label: 'TTFT mean', unit: 'ms', better: 'lower', group: 'latency' },
  { id: 'acceptance_rate', label: 'Draft acceptance rate', unit: '%', better: 'higher', group: 'speculative' },
  { id: 'concurrency', label: 'Concurrent users tested', unit: 'users', better: null, group: 'context' },
]

const revision = (id, values, custom = []) => ({ id, results: { values, custom } })

test('the best value follows each metric direction, and ties share it', () => {
  assert.deepEqual(bestOf({ a: 10, b: 30, c: 20 }, 'higher'), ['b'])
  assert.deepEqual(bestOf({ a: 10, b: 30, c: 10 }, 'lower'), ['a', 'c'])
  assert.deepEqual(bestOf({ a: 10, b: undefined, c: 12 }, 'lower'), ['a'])
})

test('nothing is best without a direction, a rival, or a difference', () => {
  assert.deepEqual(bestOf({ a: 10, b: 30 }, null), [])
  assert.deepEqual(bestOf({ a: 10, b: undefined }, 'higher'), [])
  assert.deepEqual(bestOf({ a: 10, b: 10 }, 'higher'), [])
})

test('rows cover measured metrics, context first, then custom ones by name', () => {
  const rows = compareRows(
    [
      revision('r2', { output_tps: 2100, ttft_ms: 260, concurrency: 32 }, [{ name: 'MTP acceptance', value: 71, unit: '%', better: 'higher' }]),
      revision('r1', { output_tps: 1500, ttft_ms: 300, concurrency: 32 }, [{ name: 'mtp acceptance ', value: 64, unit: '', better: null }]),
    ],
    metrics,
  )
  assert.deepEqual(rows.map((row) => row.key), ['concurrency', 'output_tps', 'ttft_ms', 'custom:mtp acceptance'])
  assert.deepEqual(rows.find((row) => row.key === 'output_tps').best, ['r2'])
  assert.deepEqual(rows.find((row) => row.key === 'ttft_ms').best, ['r2'])
  assert.deepEqual(rows.find((row) => row.key === 'concurrency').best, [])
  const custom = rows.at(-1)
  assert.equal(custom.unit, '%')
  assert.deepEqual(custom.values, { r2: 71, r1: 64 })
  assert.deepEqual(custom.best, ['r2'])
})

test('values read naturally with their units', () => {
  assert.equal(formatValue(1840.56, 'tok/s'), '1,841 tok/s')
  assert.equal(formatValue(68.54, '%'), '68.5%')
  assert.equal(formatValue(3.456, '×'), '3.46×')
  assert.equal(formatValue(4, ''), '4')
  assert.equal(formatValue(undefined, 'ms'), '—')
  assert.deepEqual(
    headlineMetrics({ values: { ttft_ms: 212, output_tps: 1840 }, custom: [] }, metrics),
    [
      { label: 'Output throughput', text: '1,840 tok/s' },
      { label: 'TTFT mean', text: '212 ms' },
    ],
  )
})
