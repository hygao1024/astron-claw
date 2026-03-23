import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight(str: string, lang: string) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        const result = hljs.highlight(str, { language: lang })
        return `<div class="code-wrapper"><div class="code-header"><span class="code-lang">${lang}</span><button class="copy-btn" onclick="navigator.clipboard.writeText(this.closest('.code-wrapper').querySelector('code').textContent).then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)})">Copy</button></div><pre><code class="hljs">${result.value}</code></pre></div>`
      } catch { /* ignore */ }
    }
    try {
      const result = hljs.highlightAuto(str)
      return `<div class="code-wrapper"><div class="code-header"><span class="code-lang">code</span><button class="copy-btn" onclick="navigator.clipboard.writeText(this.closest('.code-wrapper').querySelector('code').textContent).then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500)})">Copy</button></div><pre><code class="hljs">${result.value}</code></pre></div>`
    } catch { /* ignore */ }
    return ''
  },
})

export function renderMarkdown(content: string): string {
  if (!content) return ''
  try {
    return md.render(content)
  } catch {
    return content
  }
}

/** Simple inline render for compact areas (timeline, delegation markers) */
export function renderContent(content: string): string {
  if (!content) return ''
  return content
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br/>')
}
