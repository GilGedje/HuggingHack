import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'

import { rehypeGithubAlerts } from '../src/markdownAlerts.ts'
import { modelCardSanitizeSchema } from '../src/modelCard.ts'

const pipelines = {
  'model cards': [rehypeRaw, [rehypeSanitize, modelCardSanitizeSchema], rehypeGithubAlerts],
  'site Markdown': [rehypeSanitize, rehypeGithubAlerts],
}

function render(source, rehypePlugins) {
  return renderToStaticMarkup(
    React.createElement(ReactMarkdown, { remarkPlugins: [remarkGfm], rehypePlugins }, source),
  )
}

for (const [name, plugins] of Object.entries(pipelines)) {
  test(`${name}: GitHub alerts become titled callouts`, () => {
    const html = render('> [!TIP]\n> Use the **Q4** file.', plugins)
    assert.match(html, /<blockquote class="markdown-alert markdown-alert-tip">/)
    assert.match(html, /<p class="markdown-alert-title" data-alert="tip">Tip<\/p>/)
    assert.match(html, /<p>Use the <strong>Q4<\/strong> file.<\/p>/)
    assert.doesNotMatch(html, /\[!TIP\]/)
  })

  test(`${name}: markers ignore case and may stand alone in the first paragraph`, () => {
    const html = render('> [!caution]\n>\n> Deletes the cache.', plugins)
    assert.match(html, /markdown-alert-caution/)
    assert.match(html, /Caution<\/p>\s*<p>Deletes the cache.<\/p>/)
    assert.equal(html.match(/<p/g).length, 2)
  })

  test(`${name}: ordinary quotes and inline markers stay as written`, () => {
    assert.equal(render('> A quote', plugins), '<blockquote>\n<p>A quote</p>\n</blockquote>')
    assert.match(render('> [!NOTE] same line', plugins), /<blockquote>\n<p>\[!NOTE\] same line<\/p>/)
    assert.match(render('> [!DANGER]\n> text', plugins), /<blockquote>\n<p>\[!DANGER\]/)
  })
}

test('alerts in raw HTML cannot bring their own attributes past sanitizing', () => {
  const html = render(
    '<blockquote onclick="alert(1)" class="x">\n\n[!WARNING]\nCareful\n\n</blockquote>',
    pipelines['model cards'],
  )
  assert.match(html, /<blockquote class="markdown-alert markdown-alert-warning">/)
  assert.doesNotMatch(html, /onclick|class="x"/)
})
