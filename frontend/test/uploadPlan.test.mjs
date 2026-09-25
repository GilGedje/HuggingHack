import assert from 'node:assert/strict'
import test from 'node:test'
import { detectPrecision, isSkipped, planUpload } from '../src/uploadPlan.ts'

test('a cloned folder skips git internals, caches, and OS clutter', () => {
  const plan = planUpload([
    { path: 'config.json', size: 700 },
    { path: 'tokenizer.json', size: 9_000 },
    { path: 'README.md', size: 2_000 },
    { path: 'model-00001-of-00002.safetensors', size: 5_000_000 },
    { path: 'model-00002-of-00002.safetensors', size: 3_000_000 },
    { path: '.git/HEAD', size: 20 },
    { path: '.git/objects/ab/cdef', size: 400 },
    { path: '.cache/huggingface/download/x.lock', size: 0 },
    { path: 'sub/__pycache__/x.pyc', size: 10 },
    { path: '.DS_Store', size: 6_000 },
    { path: '.gitattributes', size: 1_500 },
  ])
  assert.deepEqual(
    plan.files.map((file) => file.path),
    ['config.json', 'tokenizer.json', 'README.md', 'model-00001-of-00002.safetensors', 'model-00002-of-00002.safetensors', '.gitattributes'],
  )
  assert.equal(plan.skipped.length, 5)
  assert.equal(plan.weightFiles, 2)
  assert.equal(plan.weightBytes, 8_000_000)
  assert.equal(plan.totalBytes, 8_013_200)
  assert.deepEqual(plan.checks, { config: true, tokenizer: true, weights: true, card: true })
})

test('missing essentials are reported, and only top-level files count', () => {
  const plan = planUpload([
    { path: 'nested/config.json', size: 1 },
    { path: 'pytorch_model.bin', size: 10 },
  ])
  assert.deepEqual(plan.checks, { config: false, tokenizer: false, weights: false, card: false })
  assert.equal(isSkipped('weights.safetensors.hugginghack-part'), true)
  assert.equal(isSkipped('.hugginghack.json'), true)
  assert.equal(isSkipped('docs/.git-notes.md'), false)
})

test('precision follows the same rules as the server', () => {
  assert.equal(detectPrecision({ torch_dtype: 'bfloat16' }), 'bf16')
  assert.equal(detectPrecision({ text_config: { dtype: 'torch.bfloat16' } }), 'bf16')
  // FP8 checkpoints keep torch_dtype bfloat16 for the layers left unquantized.
  assert.equal(detectPrecision({ torch_dtype: 'bfloat16', quantization_config: { quant_method: 'fp8' } }), 'fp8')
  assert.equal(
    detectPrecision({
      torch_dtype: 'bfloat16',
      quantization_config: { quant_method: 'compressed-tensors', format: 'nvfp4-pack-quantized' },
    }),
    'nvfp4',
  )
  assert.equal(
    detectPrecision({
      quantization_config: {
        quant_method: 'compressed-tensors',
        config_groups: { group_0: { weights: { num_bits: 8, type: 'float' } } },
      },
    }),
    'fp8',
  )
  assert.equal(detectPrecision({ torch_dtype: 'bfloat16' }, { quantization: { quant_algo: 'NVFP4' } }), 'nvfp4')
  assert.equal(
    detectPrecision({}, { quantization: { quant_algo: 'MIXED_PRECISION', quantized_layers: { a: { quant_algo: 'NVFP4' } } } }),
    'nvfp4',
  )
  assert.equal(detectPrecision({ quantization_config: { quant_method: 'unknown' }, torch_dtype: 'float16' }), 'fp16')
  assert.equal(detectPrecision({}), null)
  assert.equal(detectPrecision(null), null)
})
