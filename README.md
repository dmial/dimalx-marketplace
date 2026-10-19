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

### pdf-marks

Ставит в PDF то, что на бумаге ставят рукой: подпись и печать компании. Два скилла в одном плагине, оба сами находят блок подписи — строку имени вида `(Имя Фамилия)` и линию над ней — и рендерят PNG-кроп области для визуальной проверки. Координаты руками указывать не надо.

- **sign-pdf** — рукописная подпись на линию. Фразы: «поставь подпись», «подпиши PDF», «sign this PDF».
- **stamp-pdf** — круглая печать внахлёст на подпись, с наклоном и полупрозрачностью. Умеет вытащить печать из фотографии оттиска на бумаге. Фразы: «поставь печать», «проштампуй», «add company stamp».

Установка:

```
/plugin install pdf-marks@dimalx-marketplace
pip3 install pymupdf pillow numpy
```

Подписи и печати хранятся в папках пользователя — `~/Documents/Signatures/` и `~/Documents/Stamps/` (или `$SIGNATURES_DIR` / `$STAMPS_DIR`), **не внутри плагина**: печать компании в публичном репозитории — готовый к подделке актив. Добавить свою:

```
python3 sign.py --register ~/Downloads/scan.png -p ana
python3 prep.py ~/Downloads/IMG_1234.HEIC ~/Documents/Stamps/acme.png --rotate 90
```

Это визуальный оттиск, а не квалифицированная электронная подпись.

Подробнее: [sign-pdf](plugins/pdf-marks/skills/sign-pdf/SKILL.md), [stamp-pdf](plugins/pdf-marks/skills/stamp-pdf/SKILL.md)

### scan-to-markdown

Дословно переносит сканы бумажных документов, прежде всего медицинских, в Markdown. Каждую страницу независимо читают две модели, код сверяет их между собой и с распознаванием Apple Vision, спорные места разбирает арбитр по скану. В конце человек сверяет страницы на локальной странице сверки и оставляет комментарии, а агент превращает их в правки. Фразы: «оцифруй сканы», «сделай остальные документы», «поправь по комментариям».

Установка:

```
/plugin install scan-to-markdown@dimalx-marketplace
brew install poppler
```

Нужны macOS (Apple Vision) и Xcode Command Line Tools.

Подробнее: [scan-to-markdown](plugins/scan-to-markdown/README.md)

## Лицензия

[MIT](LICENSE)
