import assert from 'node:assert/strict'
import test from 'node:test'
import { relationGroup, relationName, relationOf } from '../src/modelTree.ts'

test('relations read as the Hub words them', () => {
  assert.equal(relationOf('quantized'), 'Quantization of')
  assert.equal(relationOf('adapter'), 'Adapter for')
  assert.equal(relationOf(null), 'Made from')
  assert.equal(relationGroup('quantized', 2), 'Quantizations')
  assert.equal(relationGroup('finetune', 1), 'Fine-tune')
  assert.equal(relationName('merge'), 'Merge')
})
