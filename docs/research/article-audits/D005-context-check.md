# D005 / Table 4: независимая сверка контекста

Дата: 21 ноября 2026. Проверка данных и чтение кода; повторный `extract`, модели и OCR не запускались. Проверенная версия кода: `b330a622971a4073cb8d28bc27ba128c21460286` (текущее рабочее дерево может содержать локальные правки).

## Вывод

Все 16 чисел Table 4 читаются в оригинале и имеют явный локальный контекст: эксперимент чувствительности к CoT-инструкциям, генератор **llama-3.3-70b-instruct**, тестовая выборка **holdout set from the Web Pages (C) dataset**, четыре варианта инструкции. Датасет задан только в абзаце приложения A, а не в шапке. Перенос Llama-3.1 из основного §5.1.3 на эти строки противоречит прямому локальному указанию приложения A. [PDF, стр. 10, приложение A и Table 4](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=10>).

Это достаточное основание для ручного контрольного набора из 16 результатов. Это **не подтверждение**, что нынешний конвейер уже создаёт эти записи: указанные в прежнем аудите артефакты прогона в доступной файловой системе не обнаружены.

## Источник и способ проверки

Оригинал: [D005.pdf](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf>), 24 страницы, SHA-256 `0c092c75df6a1fb2bb7279882979d3ab71edb52ef57a4f8280f1071fbb564616`.

Текст прочитан PyMuPDF через `/home/souppxdd/Documents/RAG/.venv/bin/python`; страницы 5, 6, 10, 11 и 15 отрисованы и просмотрены визуально. Все номера ниже относятся к порядку страниц PDF, начиная с 1; печатные номера для них — 779, 780, 784, 785 и 789 соответственно. Ни текстовое извлечение, ни этот аудит не изменяют исходный PDF.

## Явные условия и их область действия

| Поле | Подтверждённое содержание | Источник / короткая выдержка |
| --- | --- | --- |
| Эксперимент | Sensitivity to CoT Instructions; чувствительность RAG QA к вариантам CoT-инструкций | PDF 10, заголовок A и подпись Table 4: “sensitivity of RAG QA factuality metrics to CoT step instructions” |
| Генератор всех четырёх вариантов | `llama-3.3-70b-instruct` | PDF 10, A: “In these experiments, we used llama-3.3-70b-instruct”; далее прямо названа роль generative model |
| Тестовый набор | Дословно `a holdout set from the Web Pages (C) dataset` | PDF 10, A, конец первого абзаца. Сохранять название и holdout; размер и состав разбиения здесь не даны |
| Сравниваемое условие | Меняются CoT step instructions при одинаковых base instructions | PDF 10, A, второй абзац: “the only difference is CoT step instructions” |
| Prompt-A | Базовый instruction prompt G.2 | PDF 10, начало A; сам промпт — PDF 15, Figure 8 |
| Prompt-B | Шаг 1 оценивает отдельные references; шаги 2 и 3 объединены, усилена инструкция говорить “I don’t know” при недостатке информации | PDF 10, первый абзац A |
| Prompt-C | Удалён акцент на “I don’t know” | PDF 10, первый абзац A |
| Prompt-D | Удалены CoT step instructions, оставлена просьба подумать перед ответом | PDF 10, первый абзац A |
| Метрики | Accuracy, Hallucination, Missing; Factuality = Accuracy − Hallucination | PDF 10, шапка Table 4; определения PDF 5, §5.1.2, повторены PDF 11, D |

Источники таблицы условий: [PDF 10](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=10>), [PDF 15, Figure 8](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=15>), [PDF 5, §5.1.2](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=5>), [PDF 11, D](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=11>).

Оговорки протокола:

- `Web Pages (C)` — буквальное название в A. В приложении C авторы перечисляют Wiki Pages и Web Search; автоматически приравнивать Web Pages к одному из этих двух имён нельзя. Web Search на PDF 11 описывает до 10 страниц на запрос, truncation до 3000 слов и время/город как дополнительный контекст. Это описание подготовки источников, а не явный retrieval top-k тестового эксперимента A. [PDF 10–11, C](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=11>).
- §5.1.2 определяет accurate / hallucinated / missing. LLM-as-judge назван для конкретно перечисленных CRAG, HotpotQA, MS Marco, FinQA, TAT-QA, HAGRID, ExpertQA; для остальных benchmarks упомянут fact-checking tool, подобный VeriScore. Web Pages в перечне не назван. **Конкретный evaluator и его модель для A отдельно не установлены.** Не следует заполнять их по одному лишь соседству с основными экспериментами. [PDF 5, §5.1.2](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=5>).
- Основной §5.1.3 буквально заявляет Llama-3.1-70b-instruct как base model “across all our experiments”, temperature 0.2 и top-p 0.9. Для модели A даёт более узкое прямое указание Llama-3.3. Обе цитаты стоит сохранять с их областями действия. Температуру, top-p и параметры обучения из §5.1.3 в ручной контроль A не переносим: локальная применимость не установлена. [PDF 6, §5.1.3](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=6>).

## Контрольные 16 результатов

Общие условия **каждой** строки: D005; PDF 10; Table 4; эксперимент A; `llama-3.3-70b-instruct`; `holdout set from the Web Pages (C) dataset`; RAG QA; вариант Prompt-A/B/C/D. Адреса ниже — ручные координаты в сетке с единицы, включая шапку (не утверждение об индексах отсутствующего JSON). Оригинальная единица печати всех ячеек — `%`. Factuality — разность процентных долей на шкале процентных пунктов; исходный `%` сохраняется. [PDF 10, Table 4](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=10>).

| Вариант | Метрика | Исходная ячейка | Ряд, столбец |
| --- | --- | ---: | --- |
| Prompt-A | Accuracy | 69.9% | 2, 2 |
| Prompt-A | Hallucination | 18.4% | 2, 3 |
| Prompt-A | Missing | 11.7% | 2, 4 |
| Prompt-A | Factuality | 51.5% | 2, 5 |
| Prompt-B | Accuracy | 63.2% | 3, 2 |
| Prompt-B | Hallucination | 15.5% | 3, 3 |
| Prompt-B | Missing | 21.4% | 3, 4 |
| Prompt-B | Factuality | 47.7% | 3, 5 |
| Prompt-C | Accuracy | 69.2% | 4, 2 |
| Prompt-C | Hallucination | 18.7% | 4, 3 |
| Prompt-C | Missing | 12.1% | 4, 4 |
| Prompt-C | Factuality | 50.5% | 4, 5 |
| Prompt-D | Accuracy | 69.3% | 5, 2 |
| Prompt-D | Hallucination | 19.6% | 5, 3 |
| Prompt-D | Missing | 11.1% | 5, 4 |
| Prompt-D | Factuality | 49.7% | 5, 5 |

Арифметический контроль: четыре Factuality совпадают с разностью напечатанных Accuracy и Hallucination. У Prompt-B три категории дают 100.1%; сохраняем напечатанные числа, не подгоняем Missing. Возможное объяснение — округление, но авторы в этом месте его явно не обсуждают. [PDF 10, Table 4](</home/souppxdd/Documents/RAG/docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D005.pdf#page=10>).

## Сохранённый прогон: что удалось и не удалось проверить

[Прежняя карточка D005](D005.md) и [бенчмарк](../table-parsing-benchmark.md) сообщают: `D005-p10-t4-docling-tableformer`, сетка 5 × 5, прочитано 16/16, 16 замечаний, 0 записей. Адрес в этих документах: `runs/benchmark-2026-11-21.hSazlU/D005-p10-t4-docling-tableformer/run.json`.

На момент проверки этот каталог отсутствовал в рабочем дереве; `/home/souppxdd/Documents/RAG/runs` также отсутствовал. Поиск указанного имени каталога/попытки в `/home/souppxdd` не нашёл копии. Поэтому **16 замечаний / 0 записей в этом аудите — документированный предыдущий результат, не повторно проверенный JSON**. Версия движка, commit, параметры, конкретные тексты замечаний, SHA исходного PDF внутри прогона и headers числовых ячеек не установлены. Доступная `runs/snapshots/` не содержит D005. Нельзя приписывать отсутствующему артефакту нынешние ветвления кода.

## Где нынешняя привязка теряет контекст: чтение кода

Это статическая проверка текущего рабочего дерева, не трасса бенчмарка:

1. [SCOPED_MODEL и extract_condition_assertions](../../../rag_mvp/article_context/assertions.py) умеют выделить `In these experiments ... as the generative model`, но сохраняют scope буквально `In these experiments`; набор и протокол не входят в поддерживаемые поля (`generator_model`, `retrieval_top_k`). Значит, даже обнаруженная модель сама по себе не создаёт связь с Prompt-A/B/C/D и не извлекает holdout Web Pages (строки 31–35, 43–48, 67–99).
2. [bind_article_context](../../../rag_mvp/article_table_binding.py) переносит `item.scope` без разрешения указателя these experiments и без добавления TableTarget в эти основные assertions (строки 107–112). [scope_rank](../../../rag_mvp/core/domain.py) подтверждает точное имя метода или all methods/across all methods; `In these experiments` для строк Prompt-A/B/C/D даст ранг 0 (271–279). Это отдельная граница между найденной цитатой и применимостью к строкам.
3. В [column_conditions](../../../rag_mvp/core/domain.py) нет ветки для одного заголовка-метрики, когда dataset находится только в прозе: поддержаны другие формы шапки (194–268). У Table 4 четыре метрики в единственной верхней строке и варианты инструкций слева; **Web Pages отсутствует в сетке**. В `collect_results` отсутствие `conditions` приводит к общему `ambiguous-binding` до сопоставления generator (385–389). Нужен сохранённый grid, чтобы подтвердить именно такую форму `cell.headers` у исходного прогона.
4. `experiment` в [collect_results](../../../rag_mvp/core/domain.py) берётся из подписи при словах results/evaluation/comparison или специальной форме grouped_dataset (351–354). Подпись Table 4 — “Experiments to study the sensitivity ...” — не содержит этих ключей. Это дополнительный статически видимый барьер для данного названия эксперимента, независимо от модели. Там же `task` распознаётся только как multi-hop или open-domain QA (341–348); локальная формулировка RAG QA сама по себе их не подтверждает. При фактическом контексте без этих слов поле остаётся пустым и срабатывает тот же общий барьер (385–389).

Таким образом, сводить проблему к отсутствию Llama-3.3 было бы преждевременно. Источник явно задаёт модель, dataset/holdout и локальные варианты протокола, а видимые интерфейсы текущей привязки сохраняют лишь часть этих условий и не разрешают локальную область A на строки. Исправления кода, изменение модели данных и повторные прогоны в этот аудит не входят.

## Следующий контроль

Когда исходный `run.json` и сетка станут доступны, сопоставить их SHA с оригиналом, выписать фактические headers/remarks и проверить каждую из 16 строк по общей связке выше. Отдельно проверить, что источник генератора открывает **PDF 10, A**, а не PDF 6, §5.1.3; dataset ссылается на ту же локальную цитату, а retrieval top-k, размер holdout и evaluator остаются незаполненными без дополнительного первичного основания.
