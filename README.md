# dimalx-marketplace

Claude Code plugin marketplace by Dmitri Alekseev.

## Установка маркетплейса

```
/plugin marketplace add dmial/dimalx-marketplace
```

После этого можно ставить отдельные плагины:

```
/plugin install <plugin-name>@dimalx-marketplace
```

## Плагины

### missing-manual-creator

Создаёт учебные материалы (учебники, методички, конспекты, шпаргалки) по принципам **Missing Manual**: пирамидальная структура, близость термина и определения, контекстная глубина, лаконичность и практичность.

Срабатывает на фразы вроде «напиши учебник», «сделай методичку», «оформи в missing manual», «write a missing manual», «create a study guide».

Установка:

```
/plugin install missing-manual-creator@dimalx-marketplace
```

Подробнее: [plugins/missing-manual-creator](plugins/missing-manual-creator/skills/missing-manual-creator/SKILL.md)

### assinar-pdf

Ставит сохранённую рукописную подпись (PNG) в PDF. Сам находит блок подписи — строку имени вида `(Имя Фамилия)` и линию над ней — сажает штамп и рендерит PNG-кроп области для визуальной проверки. Координаты руками указывать не надо.

Срабатывает на фразы вроде «поставь подпись», «подпиши PDF», «sign this PDF», «assinar PDF».

Установка:

```
/plugin install assinar-pdf@dimalx-marketplace
pip3 install pymupdf
```

Подписи хранятся в папке пользователя — `~/Documents/Signatures/` (или `$SIGNATURES_DIR`), **не внутри плагина**. Добавить свою:

```
python3 assinar.py --registar ~/Downloads/scan.png -p ana
```

Это визуальный штамп, а не квалифицированная электронная подпись.

Подробнее: [plugins/assinar-pdf](plugins/assinar-pdf/skills/assinar-pdf/SKILL.md)

## Лицензия

[MIT](LICENSE)
