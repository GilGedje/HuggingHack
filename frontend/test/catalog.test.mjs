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
