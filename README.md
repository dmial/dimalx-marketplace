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

## Лицензия

[MIT](LICENSE)
