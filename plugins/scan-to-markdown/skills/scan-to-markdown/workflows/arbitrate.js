export const meta = {
  name: 'scan-arbitrate',
  description: 'Resolve reader A/B discrepancies by looking at the scan; writes decisions only, code builds the final text',
  whenToUse: 'Launched by the scan-to-markdown skill when run.py asks for it; args come from logs/scan-to-markdown/plan/arbitrate.json',
  phases: [
    { title: 'Arbitrate', detail: 'one arbiter per page with open discrepancies' },
  ],
}

// args: { root, cache, log, zoom, rules, reader_rules, arbiter: {model, effort},
//         pages: [{ doc, page, label, strips: [[y0, y1], ...], open }] }
// File paths follow common.page_paths in scripts/common.py.
const image = (t) => `${args.cache}/pages/${t.doc}/p${t.page}.png`
const stripPath = (t, i) => `${args.cache}/strips/${t.doc}/p${t.page}_s${i + 1}.png`
const logPath = (t, folder, suffix) => `${args.log}/${folder}/${t.doc}/p${t.page}${suffix}`

const REPORT = {
  type: 'object',
  properties: {
    decisions_path: { type: 'string' },
    decided: { type: 'integer', description: 'number of decisions written' },
    extra_findings: { type: 'integer', description: 'number of extra_findings written' },
    notes: { type: 'string', description: 'anything the human reviewer should know, or empty' },
  },
  required: ['decisions_path', 'decided', 'extra_findings', 'notes'],
}

function prompt(t) {
  const strips = t.strips.map(([y0, y1], i) => `   ${i + 1}) ${stripPath(t, i)} (пиксели ${y0}–${y1} по высоте)`).join('\n')
  return [
    'Ты — арбитр: по скану решаешь, какое из двух прочтений страницы медицинского документа верно.',
    `Корень репозитория: ${args.root}`,
    `1. Прочитай правила и следуй им буквально: ${args.rules}`,
    `   Правила читателей (обозначения и что не переносится): ${args.reader_rules}`,
    `2. Файл сравнения со списком расхождений: ${logPath(t, 'compare', '.json')}`,
    `   Решить нужно все расхождения с needs_decision: true — их ${t.open}.`,
    `3. Скан страницы: ${image(t)}`,
    '   Полосы страницы в исходном размере:',
    strips,
    `4. Тексты читателей: A — ${logPath(t, 'reads', '.A.md')}; B — ${logPath(t, 'reads', '.B.md')}`,
    `5. Инструмент увеличения: python3 "${args.zoom}" "${image(t)}" <верх %> <низ %> [<лево %> <право %>] [--scale N]`,
    '   Команда печатает путь к увеличенному фрагменту — открой его через Read.',
    `6. Решения запиши инструментом Write в файл: ${logPath(t, 'decisions', '.json')}`,
    'В конце верни короткий отчёт по схеме.',
  ].join('\n')
}

phase('Arbitrate')
const results = await parallel(args.pages.map((t) => () =>
  agent(prompt(t), {
    label: `арбитр · ${t.label}`,
    phase: 'Arbitrate',
    schema: REPORT,
    model: args.arbiter.model,
    effort: args.arbiter.effort,
  }).then((r) => ({ doc: t.doc, page: t.page, report: r }))))

const done = results.filter((r) => r && r.report)
log(`${done.length}/${args.pages.length} pages arbitrated`)
return results.map((r, i) =>
  r && r.report
    ? { doc: r.doc, page: r.page, decided: r.report.decided, extra_findings: r.report.extra_findings, notes: r.report.notes }
    : { failed_page: args.pages[i].label })
