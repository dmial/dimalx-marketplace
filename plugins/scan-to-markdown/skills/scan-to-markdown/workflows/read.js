export const meta = {
  name: 'scan-read',
  description: 'Two independent verbatim transcriptions per scan page: reader A on the full page, reader B on strips',
  whenToUse: 'Launched by the scan-to-markdown skill when run.py asks for it; args come from logs/scan-to-markdown/plan/read.json',
  phases: [
    { title: 'Read', detail: 'reader A and reader B for every page, in parallel' },
  ],
}

// args: { root, cache, log, zoom, rules, reader_a: {model, effort}, reader_b: {model, effort},
//         pages: [{ doc, page, label, height, strips: [[y0, y1], ...], need_a, need_b }] }
// File paths follow common.page_paths in scripts/common.py.
const image = (t) => `${args.cache}/pages/${t.doc}/p${t.page}.png`
const stripPath = (t, i) => `${args.cache}/strips/${t.doc}/p${t.page}_s${i + 1}.png`
const outPath = (t, reader) => `${args.log}/reads/${t.doc}/p${t.page}.${reader}.md`

const REPORT = {
  type: 'object',
  properties: {
    written_path: { type: 'string' },
    uncertain_count: { type: 'integer', description: 'how many [?…] marks are in the file' },
    illegible_count: { type: 'integer', description: 'how many [неразборчиво] marks are in the file' },
    notes: { type: 'string', description: 'anything the next steps should know, or empty' },
  },
  required: ['written_path', 'uncertain_count', 'illegible_count', 'notes'],
}

const zoomTool = (image) =>
  `python3 "${args.zoom}" "${image}" <верх %> <низ %> [<лево %> <право %>] [--scale N]`

function promptA(t) {
  return [
    'Ты — читатель A: дословно переносишь в текст одну страницу медицинского документа.',
    `1. Прочитай правила и следуй им буквально: ${args.rules}`,
    `2. Изображение страницы — твой основной источник: ${image(t)}`,
    `3. Инструмент увеличения: ${zoomTool(image(t))}`,
    '   Команда печатает путь к увеличенному фрагменту — открой его через Read.',
    `4. Результат запиши инструментом Write в файл: ${outPath(t, 'A')}`,
    `5. Не открывай другие транскрипции и вообще ничего в папке ${args.log}, кроме своего файла: чтение должно быть независимым.`,
    'В конце верни короткий отчёт по схеме.',
  ].join('\n')
}

function promptB(t) {
  const strips = t.strips
    .map(([y0, y1], i) => `   ${i + 1}) ${stripPath(t, i)} — пиксели ${y0}–${y1} по высоте из ${t.height}`)
    .join('\n')
  return [
    'Ты — читатель B: дословно переносишь в текст одну страницу медицинского документа.',
    `1. Прочитай правила и следуй им буквально: ${args.rules}`,
    '2. Страница нарезана на горизонтальные полосы с перекрытием, сверху вниз. Каждое слово и каждую цифру читай по полосам — это твой основной источник:',
    strips,
    `   Целиком страницу открой только чтобы понять раскладку (блоки, колонки, таблицы): ${image(t)}`,
    '   Полосы перекрываются: строку из зоны перекрытия записывай один раз.',
    `3. Инструмент увеличения: ${zoomTool(image(t))}`,
    '   Команда печатает путь к увеличенному фрагменту — открой его через Read.',
    `4. Результат запиши инструментом Write в файл: ${outPath(t, 'B')}`,
    `5. Не открывай другие транскрипции и вообще ничего в папке ${args.log}, кроме своего файла: чтение должно быть независимым.`,
    'В конце верни короткий отчёт по схеме.',
  ].join('\n')
}

phase('Read')
const jobs = []
for (const t of args.pages) {
  if (t.need_a) {
    jobs.push(() =>
      agent(promptA(t), {
        label: `A · ${t.label}`,
        phase: 'Read',
        schema: REPORT,
        model: args.reader_a.model,
        effort: args.reader_a.effort,
      }).then((r) => ({ reader: 'A', doc: t.doc, page: t.page, report: r })))
  }
  if (t.need_b) {
    jobs.push(() =>
      agent(promptB(t), {
        label: `B · ${t.label}`,
        phase: 'Read',
        schema: REPORT,
        model: args.reader_b.model,
        effort: args.reader_b.effort,
      }).then((r) => ({ reader: 'B', doc: t.doc, page: t.page, report: r })))
  }
}

const results = await parallel(jobs)
const done = results.filter((r) => r && r.report)
log(`${done.length}/${jobs.length} readings written`)
return results.map((r, i) =>
  r && r.report
    ? { reader: r.reader, doc: r.doc, page: r.page, uncertain: r.report.uncertain_count, illegible: r.report.illegible_count, notes: r.report.notes }
    : { failed_job: i })
