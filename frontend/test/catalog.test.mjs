import assert from 'node:assert/strict'
import test from 'node:test'
import { PARAMETER_STOPS, parameterQuery, parameterRangeLabel, precisionLabel, stopLabel } from '../src/catalog.ts'

const last = PARAMETER_STOPS.length - 1

test('the full slider range sends no parameter filter', () => {
  assert.equal(parameterQuery(0, last), '')
  assert.equal(parameterRangeLabel(0, last), 'Any size')
})

test('slider ranges become min/max filters the backend understands', () => {
  assert.equal(parameterQuery(3, 5), 'min:8B,max:32B')
  assert.equal(parameterQuery(0, 1), 'max:1B')
  assert.equal(parameterQuery(6, last), 'min:70B')
  assert.equal(parameterRangeLabel(3, 5), '8B – 32B')
  assert.equal(parameterRangeLabel(0, 3), 'Up to 8B')
  assert.equal(parameterRangeLabel(6, last), '70B and up')
  assert.equal(parameterRangeLabel(3, 3), 'About 8B')
  assert.equal(stopLabel(last), '512B+')
})

test('precision labels', () => {
  assert.equal(precisionLabel('nvfp4'), 'NVFP4')
  assert.equal(precisionLabel('int4'), 'INT4')
  assert.equal(precisionLabel(null), null)
})

test('precision filters group by width while labels stay exact', async () => {
  const { PRECISION_FILTERS } = await import('../src/catalog.ts')
  assert.deepEqual(PRECISION_FILTERS.map(([id]) => id), ['bf16', 'fp8', 'fp4'])
  assert.equal(precisionLabel('fp8'), 'FP8')
  assert.equal(precisionLabel('int8'), 'INT8')
  assert.equal(precisionLabel('mxfp4'), 'MXFP4')
})

test('Explore filters survive a trip through the address', async () => {
  const { readCatalogFilters, writeCatalogFilters } = await import('../src/catalog.ts')
  const trip = (filters) => readCatalogFilters(new URLSearchParams(writeCatalogFilters(filters, new URLSearchParams()).toString()))
  const full = { tasks: [], precision: [], hardware: [], size: [0, last] }
  for (const size of [[0, last], [3, 5], [0, 0], [0, 3], [6, last], [last, last], [4, 4]]) {
    assert.deepEqual(trip({ ...full, size }).size, size)
  }
  const chosen = { tasks: ['text-generation', 'any-to-any'], precision: ['fp8'], hardware: ['h100'], size: [3, 5] }
  assert.deepEqual(trip(chosen), chosen)
  const written = writeCatalogFilters(chosen, new URLSearchParams('search=qwen&task=old&base_model=a/b'))
  assert.equal(written.get('search'), 'qwen')
  assert.equal(written.get('base_model'), 'a/b')
  assert.equal(written.get('task'), 'text-generation,any-to-any')
  assert.equal(written.get('size'), '8B-32B')
  assert.equal(writeCatalogFilters(full, written).toString(), 'search=qwen&base_model=a%2Fb')
})

test('the search box writes its text to the address, keeping everything else', async () => {
  const { writeCatalogSearch } = await import('../src/catalog.ts')
  const current = new URLSearchParams('search=qwen&task=text-generation&sort=name')
  assert.equal(writeCatalogSearch('  smol lm ', current).toString(), 'search=smol+lm&task=text-generation&sort=name')
  // Clearing the box (the × button) removes the search, not the filters.
  assert.equal(writeCatalogSearch('', current).toString(), 'task=text-generation&sort=name')
  assert.equal(writeCatalogSearch('   ', current).toString(), 'task=text-generation&sort=name')
  assert.equal(current.get('search'), 'qwen')
})

test('unreadable filters in the address count as unset', async () => {
  const { readCatalogFilters } = await import('../src/catalog.ts')
  assert.deepEqual(readCatalogFilters(new URLSearchParams('size=huge-tiny&task=,,')), {
    tasks: [],
    precision: [],
    hardware: [],
    size: [0, last],
  })
  assert.deepEqual(readCatalogFilters(new URLSearchParams('size=32B-8B')).size, [0, last])
  assert.deepEqual(readCatalogFilters(new URLSearchParams('size=70b-')).size, [6, last])
})
