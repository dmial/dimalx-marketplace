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
const notesPath = (t, reader) => `${args.log}/reads/${t.doc}/p${t.page}.${reader}.notes.md`

const REPORT = {
  type: 'object',
  properties: {
    written_path: { type: 'string' },
    uncertain_count: { type: 'integer', description: 'how many [?…] marks are in the file' },
    illegible_count: { type: 'integer', description: 'how many [неразборчиво] marks are in the file' },
    notes: { type: 'string', description: 'the same notes as in the notes file, in Russian, or empty' },
  },
  required: ['written_path', 'uncertain_count', 'illegible_count', 'notes'],
}

const zoomTool = (image) =>
  `python3 "${args.zoom}" "${image}" <верх %> <низ %> [<лево %> <право %>] [--scale N]`

const finish = (t, reader) => [
  `4. Результат запиши инструментом Write в файл: ${outPath(t, reader)}`,
  `5. Заметки для человека, который будет сверять страницу (правила, раздел 6), запиши инструментом Write в файл: ${notesPath(t, reader)}`,
  '   Заметок нет — запиши этот файл пустым.',
  `6. Не открывай другие транскрипции и вообще ничего в папке ${args.log}, кроме этих двух своих файлов: чтение должно быть независимым.`,
  'В конце верни короткий отчёт по схеме.',
]

function promptA(t) {
  return [
    'Ты — читатель A: дословно переносишь в текст одну страницу документа.',
    `1. Прочитай правила и следуй им буквально: ${args.rules}`,
    `2. Изображение страницы — твой основной источник: ${image(t)}`,
    `3. Инструмент увеличения: ${zoomTool(image(t))}`,
    '   Команда печатает путь к увеличенному фрагменту — открой его через Read.',
    ...finish(t, 'A'),
  ].join('\n')
}

function promptB(t) {
  const strips = t.strips
    .map(([y0, y1], i) => `   ${i + 1}) ${stripPath(t, i)} — пиксели ${y0}–${y1} по высоте из ${t.height}`)
    .join('\n')
  return [
    'Ты — читатель B: дословно переносишь в текст одну страницу документа.',
    `1. Прочитай правила и следуй им буквально: ${args.rules}`,
    '2. Страница нарезана на горизонтальные полосы с перекрытием, сверху вниз. Каждое слово и каждую цифру читай по полосам — это твой основной источник:',
    strips,
    `   Целиком страницу открой только чтобы понять раскладку (блоки, колонки, таблицы): ${image(t)}`,
    '   Полосы перекрываются: строку из зоны перекрытия записывай один раз.',
    `3. Инструмент увеличения: ${zoomTool(image(t))}`,
    '   Команда печатает путь к увеличенному фрагменту — открой его через Read.',
    ...finish(t, 'B'),
  ].join('\n')
}

phase('Read')
const jobs = []
for (const t of args.pages) {
  if (t.need_a) jobs.push({ t, reader: 'A', prompt: promptA(t), opts: args.reader_a })
  if (t.need_b) jobs.push({ t, reader: 'B', prompt: promptB(t), opts: args.reader_b })
}

const results = await parallel(jobs.map((job) => () =>
  agent(job.prompt, {
    label: `${job.reader} · ${job.t.label}`,
    phase: 'Read',
    schema: REPORT,
    model: job.opts.model,
    effort: job.opts.effort,
  })))
const done = results.filter(Boolean)
log(`${done.length}/${jobs.length} readings written`)
return results.map((r, i) => {
  const { t, reader } = jobs[i]
  return r
    ? { reader, doc: t.doc, page: t.page, uncertain: r.uncertain_count, illegible: r.illegible_count, notes: r.notes }
    : { failed: `reader ${reader} · ${t.label}`, reader, doc: t.doc, page: t.page }
})
