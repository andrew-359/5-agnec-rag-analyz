# Эталон метрики из подписи — P012 и P062

Снимок кода `5c4d25f`. Четыре значения размечены по оригинальным PDF, затем сверены с сохранёнными сетками и общим текстом. Страницы физические, с 1. Адрес `(строка, колонка)` дан в индексах сетки, с 0. Исходные числа сохраняют напечатанные десятичные разряды. [Оригинал P012, стр. 5](/home/andrew/dev/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/test/P012.pdf) · [Оригинал P062, стр. 15](/home/andrew/dev/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/test/P062.pdf)

## Четыре записи

| PDF и адрес | Исходное число | Строка | Полная шапка | Датасет | Метрика | Единица | Условие модели и среза |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P012, стр. 5, Table 2, `(3,1)` | `58.12` | `RP` (напечатано курсивом, в сетке `𝑅𝑃`) | `NQ → L3.1-8B` | NQ (Natural Questions) | Accuracy ответа в RAG | `%` | Генератор Llama3.1-8B-Instruct, второй уровень шапки |
| P012, стр. 5, Table 2, `(3,2)` | `58.41` | `RP` (в сетке `𝑅𝑃`) | `NQ → Q3-8B` | NQ (Natural Questions) | Accuracy ответа в RAG | `%` | Генератор Qwen3-8B, второй уровень шапки |
| P062, стр. 15, Table 7, `(2,1)` | `39.40` | `Non-RAG` | `BLEnD → am` | BLEnD | Accuracy на cultural QA | Открыта | Генератор Llama-3.2-1B из подписи. Срез исходного языка `am` (амхарский, Эфиопия) из шапки |
| P062, стр. 15, Table 7, `(2,2)` | `49.01` | `Non-RAG` | `BLEnD → ar` | BLEnD | Accuracy на cultural QA | Открыта | Тот же генератор из подписи. Срез исходного языка `ar` (арабский, Алжир) из шапки |

## Основания из PDF

### P012, Table 2

- **Метрика и шкала.** Подпись на стр. 5 прямо говорит «RAG accuracy (%)» и поясняет группировку по датасету и LLM. Знак процента относится к обоим значениям. [Фрагмент подписи `adb2be220e6f-p5-text-3`](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P012/01/article-text.json:968)
- **Датасет и задача.** Верхний заголовок обеих ячеек — `NQ`. §4.1, *Source Datasets*, раскрывает его как Natural Questions из KILT и описывает фактологический QA. В *Candidate Pool Option* NQ входит в три single-hop датасета этого эксперимента. [Source Datasets](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P012/01/article-text.json:1052) · [Candidate Pool Option](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P012/01/article-text.json:1124)
- **Строка и генератор.** Заголовок левой колонки — `Evidence`. §4.1 раскрывает `RP` как *Top-20 Retrieved Passages*. В абзацах *Retriever* и *Analyzed LLMs* указаны BGE-M3 для поиска и расшифровки моделей `L3.1-8B` и `Q3-8B`. У двух чисел общие NQ, строка RP, тип свидетельств, ретривер, метрика и единица. Из условий шапки меняется модель ответа. [Retriever и Analyzed LLMs](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P012/01/article-text.json:1088) · [Определение RP](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P012/01/article-text.json:1124)

### P062, Table 7

- **Метрика и генератор.** Подпись на стр. 15 — «Accuracy on cultural QA benchmarks with Llama-3.2-1B for a generator». Она задаёт Accuracy и Llama-3.2-1B для всей Table 7. Appendix D.1 описывает семейство LLaMA-3.2-{1B, 3B}-Instruct в составе экспериментов Tables 6–12. Соседняя Table 8 уже имеет в своей подписи Llama-3.2-3B. [Подпись `504d4a03bbec-p15-text-15`](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P062/01/article-text.json:3979) · [Appendix D.1](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P062/01/article-text.json:3619)
- **Строка, датасет и срез.** `Non-RAG` — метод прямого ответа без внешнего поиска, по определению на стр. 4. Верхний заголовок — `BLEnD`. Статья использует англоязычные вопросы MCQ и разделяет их по исходным языково-культурным сообществам. Table 4 на стр. 13 явно сопоставляет `am` с Amharic / Ethiopia и `ar` с Arabic / Algeria. При переходе с `39.40` на `49.01` меняется срез, а Non-RAG, BLEnD, задача, метрика и генератор общие. [Определение Non-RAG](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P062/01/article-text.json:835) · [Описание BLEnD MCQ](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P062/01/article-text.json:775) · [Исходные языки в Table 4](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P062/01/article-text.json:3355)
- **Шкала открыта.** В числе, шапке и подписи Table 7 отсутствует знак `%`. В описании результатов §5.1 и Appendix D.1 не найдено явного объявления единицы для Table 7. Обозначения `%p` в обсуждении улучшений относятся к разности scores. Эталон сохраняет `39.40` и `49.01` с пустой единицей и без пересчёта.

## Сверка с сохранённым чтением

У всех четырёх совпали исходное число, адрес, строка и порядок заголовков. Источник числа в каждом случае содержит рамку на указанной физической странице. ID строк и заголовков локальны для соответствующей таблицы. `caption` — ID фрагмента подписи внутри сетки, длинный ID — фрагмент подписи в общем тексте.

| PDF, адрес, число | Ячейка числа | Строка и заголовок оси | Верхний → нижний заголовок | Подпись в сетке и общем тексте |
| --- | --- | --- | --- | --- |
| P012 `(3,1)`, `58.12` | `docling-3-1` | `docling-3-0` RP, `docling-0-0` Evidence | `docling-0-1` NQ → `docling-1-1` L3.1-8B | `caption`, `adb2be220e6f-p5-text-3` |
| P012 `(3,2)`, `58.41` | `docling-3-2` | `docling-3-0` RP, `docling-0-0` Evidence | `docling-0-1` NQ → `docling-1-2` Q3-8B | `caption`, `adb2be220e6f-p5-text-3` |
| P062 `(2,1)`, `39.40` | `docling-2-1` | `docling-2-0` Non-RAG, `docling-0-0` Method | `docling-0-1` BLEnD → `docling-1-1` am | `caption`, `504d4a03bbec-p15-text-15` |
| P062 `(2,2)`, `49.01` | `docling-2-2` | `docling-2-0` Non-RAG, `docling-0-0` Method | `docling-0-1` BLEnD → `docling-1-2` ar | `caption`, `504d4a03bbec-p15-text-15` |

В P012 заголовок NQ перекрывает четыре столбца, в P062 BLEnD перекрывает 13. У четырёх ячеек `verified_condition_sources` пуст. Источники строк, путей и подписей в сетках доступны для будущей привязки. SHA-256 PDF, соответствующей сетки и `article-text.json` совпал для каждой статьи. [Сетка P012](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P012/01/grid-423cd568-906e-4573-b6d0-878172180f9f-1.json) · [Сетка P062](/home/andrew/dev/RAG/runs/test-auto-15.xlQ2Rz/P062/01/grid-00830bfe-365f-47f0-837f-e9e6563dcb3c-1.json)

## Критерии сверки адаптера

1. Роль нижней шапки проверяется по статье. У P012 `L3.1-8B` и `Q3-8B` задают генератор. У P062 `am` и `ar` задают срез, генератор для обоих один и тот же.
2. Нижний заголовок привязывается к адресу числа. `58.41` относится к Q3-8B, `49.01` — к `ar`. Назначение им `L3.1-8B` или `am` из соседней колонки — ошибка.
3. Единица подтверждается для каждой таблицы. У P012 обе записи имеют `%`; у P062 у обеих единица открыта. Обозначение Accuracy само по себе единицу не задаёт.
