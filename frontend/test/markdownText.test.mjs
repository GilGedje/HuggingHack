import assert from 'node:assert/strict'
import test from 'node:test'
import { applyFormat, markdownSummary } from '../src/markdownText.ts'

test('a summary is the first line in plain words', () => {
  assert.equal(markdownSummary('## About **NVIDIA**\n\nMore'), 'About NVIDIA')
  assert.equal(markdownSummary('\n---\n- [Visit us](https://nvidia.com) for `fp8` models'), 'Visit us for fp8 models')
  assert.equal(markdownSummary('![logo](x.png)\n\nReal text'), 'logo')
  assert.equal(markdownSummary(''), '')
  assert.equal(markdownSummary('a'.repeat(300), 10), `${'a'.repeat(9)}…`)
})

test('bold, italic, code, and links wrap the selection or a placeholder', () => {
  assert.deepEqual(applyFormat('make this bold', 10, 14, 'bold'), { text: 'make this **bold**', start: 12, end: 16 })
  assert.deepEqual(applyFormat('ab', 1, 1, 'italic'), { text: 'a_italic text_b', start: 2, end: 13 })
  const link = applyFormat('see docs', 4, 8, 'link')
  assert.equal(link.text, 'see [docs](https://)')
  assert.equal(link.text.slice(link.start, link.end), 'https://')
})

test('headings and lists prefix every selected line', () => {
  const text = 'one\ntwo\nthree'
  assert.equal(applyFormat(text, 0, 7, 'bullets').text, '- one\n- two\nthree')
  assert.equal(applyFormat(text, 5, 5, 'numbers').text, 'one\n1. two\nthree')
  assert.equal(applyFormat(text, 9, 9, 'heading').text, 'one\ntwo\n## three')
})
