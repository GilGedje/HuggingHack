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

test('a picture is cropped to its middle square', async () => {
  const { centerSquare } = await import('../src/avatarImage.ts')
  assert.deepEqual(centerSquare(400, 300), [50, 0, 300])
  assert.deepEqual(centerSquare(300, 500), [0, 100, 300])
  assert.deepEqual(centerSquare(256, 256), [0, 0, 256])
})
