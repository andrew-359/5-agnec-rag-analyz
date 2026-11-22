# Локальные прогоны разбора таблиц ВИНИТИ

20 ноября 2026 года. Для каждого способа чтения взяли одну подходящую таблицу из `dev`. Проверяли видимый текст, положение ячеек и смысловые связи по исходному PDF. `dev_gold` использовали после сохранения выхода для сверки результатов. Числа в строках сводки относятся к разным таблицам и ключам проверки. Время дано для воспроизведения запусков на Intel Core i7-14650HX. Оно зависит от инициализации движков и состава измеряемого этапа. [Выбор контрольных таблиц](table-parsing-options.md) · [Комплект и эталон](../context/viniti.md)

| Способ чтения | Вход | Проверенный выход | Время |
| --- | --- | --- | --- |
| Docling + TableFormer Accurate | D001, стр. 6, Table 1 | 18/18 результатов ThreadRAG с правильным путём шапки и координатами числа. У двух заголовков прямоугольник обрезан. | 12,39 с за весь вызов CLI |
| pdfplumber `text/text` | D008, стр. 14, Table 4 | Плоская шапка, 5 строк и 10/10 значений в своих ячейках при заданной области. | 0,099 с чтение, 0,218 с с рендером и правилами |
| Docling + RapidOCR + геометрия | D013, стр. 6, Table 2 | 72 числовые пары в сетке `8 × 9`, 144/144 связей по ключу пробы. Границы ячеек оценочные. | 12,73 с инициализация и чтение, около 4 мс восстановление |
| Unstructured `hi_res` | D021, стр. 11, Table 1 | Область таблицы найдена автоматически. Строго совпали 2/9 числовых ячеек в первом проходе и 7/9 после настройки OCR. В шапке остались ошибки. | 8,60 с, после настройки 7,96–8,06 с за `partition_pdf` при 350 dpi |

## Условия воспроизведения

Команды ниже запускаются из корня RAG. Скрипты проб, сырой выход, изображения и логи лежат локально в `/tmp/rag-table-pilots.K5MzkF/`. Снимки содержат материалы комплекта участника. Для повторного запуска Docling нужно указать новый каталог `--out`. [Правила хранения комплекта](../context/viniti.md#где-разместить-файлы) · [Запись прогона в CLI](../../rag_mvp/__main__.py#L36)

```bash
corpus="$PWD/docs/context/RAG_ВИНИТИ_участникам_2026-11-20"
pilots=/tmp/rag-table-pilots.K5MzkF
pilot_python=/tmp/rag-viniti-mvp-venv/bin/python
```

## Docling + TableFormer на D001

Запустили текущий CLI с Docling `2.151.1`, Docling Core `2.108.0` и PyPDFium2 `5.15.2`. На входе страница 6, OCR выключен, TableFormer `ACCURATE` с сопоставлением текста PDF ячейкам, CPU с лимитом четыре потока. Сырая таблица имеет размер `19 × 19`, в том числе строки шапки. Docling отдаёт текст, признаки заголовков, диапазоны строк и столбцов и прямоугольники. Адаптер строит путь по диапазонам заголовков, а предметные правила назначают его уровням генератор, датасет и метрику. CLI выпустил 252 результата и 0 замечаний. Детальная сверка ниже охватывает строку ThreadRAG. [Исходная страница](../context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D001.pdf#page=6) · [Настройки и адаптер](../../rag_mvp/strategies/parser.py#L24) · [Правила интерпретации](../../rag_mvp/core/domain.py#L69) · [Исходник TableFormer в Docling](https://github.com/docling-project/docling/blob/v2.151.1/docling/models/stages/table_structure/table_structure_model.py)

Для всех 18 результатов сверили метод, три роли заголовков и десятичное значение с `dev_gold`. Независимое чтение текста PDF внутри каждого числового прямоугольника подтвердило 18 из 18 значений. Наложение на растровую страницу проверили визуально. Два прямоугольника заголовков подрезают конец надписи при проверке ограниченной области. Для `2Wiki` читается `2Wi`, для `MuSiQue` читается `MuSiQ`. Текст этих заголовков в сырой таблице и привязки значений совпали. [Координаты и прямая сверка](</tmp/rag-table-pilots.K5MzkF/docling/verification.json>) · [Наложение на PDF](</tmp/rag-table-pilots.K5MzkF/docling/threadrag-detail.png>)

| Строка эталона | Путь для `.586` | Столбец с 0 | Прямоугольник в пунктах от верхнего левого угла |
| --- | --- | ---: | --- |
| `R241` | `Qwen3.5 plus → HotpotQA → EM` | 7 | `[257.062, 276.880, 274.606, 286.289]` |
| `R249` | `GPT-4o mini → 2Wiki → EM` | 15 | `[435.161, 277.097, 452.605, 286.538]` |

```bash
env HF_HUB_OFFLINE=1 "$pilot_python" -B -m rag_mvp extract "$corpus/dev/D001.pdf" \
  --page 6 --table 1 --out "$pilots/docling/run-replay"
"$pilot_python" "$pilots/docling/check-d001.py" --run "$pilots/docling/run-replay" \
  --gold "$corpus/dev_gold" --pdf "$corpus/dev/D001.pdf" --out "$pilots/docling/check-replay"
```

[Сырой JSON Docling](</tmp/rag-table-pilots.K5MzkF/docling/run/docling.json>) · [Превью страницы](</tmp/rag-table-pilots.K5MzkF/docling/run/page.png>) · [Сохранённый прогон](</tmp/rag-table-pilots.K5MzkF/docling/run/run.json>) · [Команда и время исходного запуска](</tmp/rag-table-pilots.K5MzkF/docling/time.txt>) · [Версии и ревизии весов](</tmp/rag-table-pilots.K5MzkF/docling/README.md>)

## pdfplumber на D008

Использовали pdfplumber `0.11.10`, pdfminer.six `20260107`, PyPDFium2 `5.15.2`, Python `3.12.3`. На странице 14 задали область `(75, 70, 285, 144)` в пунктах PDF в порядке `x0, top, x1, bottom`. Вызвали `find_tables` с обеими стратегиями `text` и `min_words_vertical=2`. Остальные параметры оставили библиотечными. Выход содержит три столбца, шапку, пять строк данных и одну пустую строку-разделитель. В сырой шапке пробелы между словами и скобками склеены. Для подписи использовали вторую вырезку `(70, 143, 292, 178)`. [Исходная страница](../context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D008.pdf#page=14) · [Код извлечения таблиц pdfplumber](https://github.com/jsvine/pdfplumber/blob/v0.11.10/pdfplumber/table.py)

Правила пробы убрали пустую строку, разобрали заголовки со шкалой секунд и связали каждое число с длиной промпта и методом. Подпись задаёт метрику задержки. Проверили `R216–R225` по длине промпта, методу, метрике, исходному числу, единице и значению после перевода в миллисекунды. Привязку длины к каждой строке выписали по PDF для сверки, поскольку эталон задаёт список длин на уровне эксперимента. Например, `100` токенов / `Baseline` даёт `26.7219` с и `26721.9` мс. Все 10 чисел лежат в своих отдельных прямоугольниках по проверке извлечённых слов PDF и наложению на страницу. Генератор и датасет для этого эксперимента оставлены неизвестными. [Наложение на PDF](</tmp/rag-table-pilots.K5MzkF/pdfplumber/overlay.png>) · [Сверка](</tmp/rag-table-pilots.K5MzkF/pdfplumber/check.json>)

```bash
"$pilot_python" "$pilots/pdfplumber/probe.py" read "$corpus/dev/D008.pdf"
"$pilot_python" "$pilots/pdfplumber/probe.py" check "$corpus/dev_gold"
```

[Сырой JSON и координаты](</tmp/rag-table-pilots.K5MzkF/pdfplumber/raw.json>) · [Превью страницы](</tmp/rag-table-pilots.K5MzkF/pdfplumber/page.png>) · [Записи после правил](</tmp/rag-table-pilots.K5MzkF/pdfplumber/derived.json>) · [Версии и время](</tmp/rag-table-pilots.K5MzkF/pdfplumber/run-info.json>)

## RapidOCR и восстановление по координатам на D013

Запустили Docling `2.151.1`, Docling Core `2.108.0`, RapidOCR `3.10.0`, PyTorch `2.8.0+cpu` на четырёх ядрах. Настройки RapidOCR `backend="torch"`, `lang=["en"]`, `mode=FULL_PAGE`, `scale=3`. Docling сохранил промежуточный разбор страницы через `generate_parsed_pages=True`. OCR дал 201 текстовый фрагмент, из них центр 93 пришёлся на область Table 2. TableFormer Accurate нашёл область и подпись, но склеил восемь строк методов в таблицу `2 × 10`. Отдельный запуск V2 на этой же странице занял 14,17 с и сохранил тот же сырой OCR. Его сетка `10 × 10` содержит сдвиги и 22 ячейки с несколькими числовыми парами. Например, `19.1 (.24)` для Search-R1 PPO, top-k 5, HotpotQA попало в следующую строку. [Исходная страница](../context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D013.pdf#page=6) · [Код сохранения OCR-страницы](https://github.com/docling-project/docling/blob/v2.151.1/docling/pipeline/standard_pdf_pipeline.py#L760-L775) · [Параметры и логи](</tmp/rag-table-pilots.K5MzkF/rapidocr/README.md>)

Правила пробы взяли область и подпись Docling, сопоставили числовые пары с девятью нижними заголовками по X и восемью строками методов по Y с поправкой на положение колонки на скане. Верхняя шапка задала группы top-k 2, 3 и 5. Число строк и столбцов проверили по растру. Все 72 центра пар попали в свои расчётные ячейки. Контуры ячеек рассчитаны по координатам OCR, их точность по независимой разметке границ не измеряли. OCR-прямоугольники всех 72 пар пересекают расчётные границы строк. При показе на PDF полезно различать подсветку фрагмента текста и границу восстановленной ячейки. [Наложение на растр](</tmp/rag-table-pilots.K5MzkF/rapidocr/d013-p6-grid-overlay.png>) · [Сетка и фрагменты](</tmp/rag-table-pilots.K5MzkF/rapidocr/d013-p6-reconstructed.json>)

После сохранения сетки сверили мультимножество ключей `(метод, retrieval top-k, датасет, вид статистики, Decimal-значение)` с `dev_gold`. Получили 144/144, без пропусков или лишних связей по этому ключу. У `OAS / top-k 5 / HotpotQA` среднее `24.5` и выборочное SD `.29` имеют общий OCR-фрагмент `24.5 (.29)` с прямоугольником `[473.56, 416.33, 504.00, 428.64]` в пунктах исходной страницы. Подпись задаёт EM в процентах, генератор Qwen2.5-3B-Instruct и две повторности оценки. [Сверка с эталоном](</tmp/rag-table-pilots.K5MzkF/rapidocr/d013-p6-evaluation.json>)

```bash
bash "$pilots/rapidocr/run.sh" "$corpus/dev/D013.pdf" accurate
bash "$pilots/rapidocr/run.sh" "$corpus/dev/D013.pdf" v2
pdftoppm -f 6 -l 6 -singlefile -r 144 -png "$corpus/dev/D013.pdf" "$pilots/rapidocr/d013-p6-page"
"$pilot_python" "$pilots/rapidocr/reconstruct.py"
"$pilot_python" "$pilots/rapidocr/evaluate.py" "$corpus/dev_gold"
```

[Сырой JSON Docling](</tmp/rag-table-pilots.K5MzkF/rapidocr/d013-p6-accurate.document.json>) · [Сырые OCR-фрагменты](</tmp/rag-table-pilots.K5MzkF/rapidocr/d013-p6-accurate.parsed.json>) · [Превью страницы](</tmp/rag-table-pilots.K5MzkF/rapidocr/d013-p6-page.png>) · [Настройки и время](</tmp/rag-table-pilots.K5MzkF/rapidocr/d013-p6-accurate.run.json>)

## Unstructured на D021

Исходная страница 11 — скан. На растре в Table 1 сверили три строки `TRUE`, `SQuAD`, `PubmedQA` и по три значения для Deepchecks, RAGAS и Langsmith. У RAGAS и Langsmith в шапке указан оценщик GPT-4o. ROC-AUC назван в абзаце §4.1 над таблицей. В `dev_gold` записи `R001–R009` используют `SQuAD-G` для напечатанного в строке `SQuAD`. Для проверки OCR берём печатную форму, каноническое имя относится к интерпретации. [Исходная страница](../context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D021.pdf#page=11) · [Превью](</tmp/rag-table-pilots.K5MzkF/unstructured/D021-11.png>)

Проверили Unstructured `0.18.32` с `unstructured-inference 1.2.0` и Tesseract `5.3.4` в отдельном локальном окружении. После скачивания зависимостей и проверки SHA-256 весов обработка шла офлайн. Вход — извлечённая без растрирования страница PDF, исходный номер 11 передан через `starting_page_number`. Настройки первого запуска `hi_res`, `infer_table_structure=True`, `yolox_quantized`, 350 dpi, английский Tesseract для страницы и таблицы, `TABLE_IMAGE_CROP_PAD=0`, четыре ядра CPU. Получили 32 элемента, из них одну таблицу и отдельный фрагмент с `ROC-AUC`. Подпись с номером пришла отдельным элементом `FigureCaption`. [Документация `partition_pdf`](https://docs.unstructured.io/open-source/core-functionality/partitioning) · [Ревизия YOLOX](https://huggingface.co/unstructuredio/yolo_x_layout/tree/7680d6f857780bcf8d49916aa2e8881bd49dee3e) · [Ревизия Table Transformer](https://huggingface.co/microsoft/table-transformer-structure-recognition/tree/f4d4bdc85c3fe4b1fa49658882a5d38bbdd0f343) · [Настройки первого прогона](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-350/run.json>)

Выходная сетка `5 × 4` разбила печатную шапку на две строки. Из девяти чисел правильно пришли `0.92` и `0.84` в столбце Deepchecks. `SQuAD` читается, `TRUE` стало `TRUP`, у `PubmedQA` появился символ `|`. У RAGAS вышло `GPT-40` вместо `GPT-4o`, а в заголовке Langsmith остались только `Answer Faithfulness`. По центрам чисел, отмеченным вручную на растре, 6 из 9 попали в рамки соответствующих ячеек. Все три центра столбца Langsmith оказались вне ожидаемых рамок, что видно на наклонённой таблице. Полная предметная запись требует отдельного подтверждения шапки и связи с абзацем ROC-AUC. [HTML таблицы](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-350/table-1.html>) · [Наложение на PDF](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-350/table-bboxes.png>) · [Сверка](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-350/verification.json>)

В двух дополнительных проходах задали `--psm 6` только для OCR вырезки таблицы. С нулевым отступом и с отступом 16 пикселей строго совпали 7 из 9 числовых ячеек, хотя сбои пришлись на разные ячейки. При отступе 16 пикселей для `TRUE / Langsmith` вместо `0.63` вышло `ies`, а у `PubmedQA / Langsmith` число `0.78` пришло вместе с текстом подписи. В обоих проходах все 9 визуальных центров попали в соответствующие прямоугольники, хотя рамки пересекают печатные границы. `TRUE` и `SQuAD` прочитались, в `PubmedQA|` остался лишний знак. Часть шапки потеряна, модель `GPT-4o` читается с ошибками. Ячейка может иметь пригодные для подсветки координаты и при этом потребовать повторного чтения. [Сверка без отступа](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-psm6/verification.json>) · [Сырой выход с отступом](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-psm6-pad16/elements.json>) · [Наложение](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-psm6-pad16/table-bboxes.png>) · [Сверка с отступом](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-psm6-pad16/verification.json>)

В обычном выходе `metadata.coordinates` содержит физическую область всей таблицы в пикселях растра страницы `2975 × 3850`, а `table_as_cells` содержит логические `x/y/w/h`. Для проверки отдельных рамок в скрипте пробы перехватили низкоуровневый выход Table Transformer. Его `bbox` задан в пикселях вырезки. После прибавления смещения вырезки и деления на размеры растра получаем доли исходной страницы. Для будущего просмотрщика штатный выход позволяет подсветить всю таблицу. Подсветка отдельной ячейки требует низкоуровневого выхода и проверки положения. [Сырой JSON элементов](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-350/elements.json>) · [Координаты модели](</tmp/rag-table-pilots.K5MzkF/unstructured/result-hires-350/table-transformer-cells.json>) · [Исходник табличной модели](https://github.com/Unstructured-IO/unstructured-inference/blob/1.2.0/unstructured_inference/models/tables.py)

```bash
pdfseparate -f 11 -l 11 "$corpus/dev/D021.pdf" "$pilots/unstructured/D021-page-%d.pdf"
pdftoppm -f 11 -l 11 -singlefile -r 200 -png "$corpus/dev/D021.pdf" "$pilots/unstructured/D021-11"
TABLE_IMAGE_CROP_PAD=0 bash "$pilots/unstructured/run-local.sh" hi_res venv-minimal yolox_quantized result-hires-350
TABLE_TESSERACT_PSM=6 TABLE_IMAGE_CROP_PAD=0 \
  bash "$pilots/unstructured/run-local.sh" hi_res venv-minimal yolox_quantized result-hires-psm6
TABLE_TESSERACT_PSM=6 TABLE_IMAGE_CROP_PAD=16 \
  bash "$pilots/unstructured/run-local.sh" hi_res venv-minimal yolox_quantized result-hires-psm6-pad16
unstructured_pilot="$pilots/unstructured"
for run in result-hires-350 result-hires-psm6 result-hires-psm6-pad16; do
  "$unstructured_pilot/venv-minimal/bin/python" "$unstructured_pilot/verify-pilot.py" \
    "$unstructured_pilot/$run" "$unstructured_pilot/D021-11.png" "$corpus/dev_gold/results.csv"
done
```

Установка Unstructured `0.27.37` не завершилась из-за сетевых тайм-аутов. Рабочая проба на `0.18.32` использовала существующие CPU PyTorch и `opencv-python-headless` из окружения Docling через `.pth` и остальные зависимости из локальных колёс. `pip check` сообщает о замене пакета OpenCV и об отсутствующем облачном клиенте, который не участвовал в локальной обработке. Исследование подтверждает результат для этого окружения. [Версии, установка и команды](</tmp/rag-table-pilots.K5MzkF/unstructured/RESULT.md>) · [Лог прогона](</tmp/rag-table-pilots.K5MzkF/unstructured/run-hires-350.log>) · [Скрипт запуска](</tmp/rag-table-pilots.K5MzkF/unstructured/run-local.sh>) · [Скрипт сверки](</tmp/rag-table-pilots.K5MzkF/unstructured/verify-pilot.py>)

### Запуск D021 через CLI

Стратегия `unstructured` запускает отдельный Python, указанный в `RAG_UNSTRUCTURED_PYTHON`. Подготовьте в этом окружении Unstructured `0.18.32` с поддержкой PDF и `unstructured-inference 1.2.0`. Утилиты Poppler (`pdfseparate`, `pdfinfo`, `pdftoppm`) и Tesseract с языком `eng` должны быть доступны через `PATH`. Для Python-пакета `python-magic` понадобится системный `libmagic`. Поместите `punkt_tab` и `averaged_perceptron_tagger_eng` в `NLTK_DATA`. [Версии проверенного окружения](</tmp/rag-table-pilots.K5MzkF/unstructured/requirements-minimal-versions.txt>)

Для повторения на чистом Python 3.12 можно установить CPU-версии PyTorch, а затем пакеты для PDF. Этот способ установки приведён для подготовки отдельного окружения. Проверенный ниже прогон использует сохранённое окружение пилота.

```bash
python3.12 -m venv "$unstructured_env"
"$unstructured_env/bin/python" -m pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cpu
"$unstructured_env/bin/python" -m pip install 'unstructured[pdf]==0.18.32' 'unstructured-inference==1.2.0' \
  'transformers==5.23.0' 'onnxruntime==1.32.0' 'Pillow==13.0.0' 'unstructured.pytesseract==0.3.15'
"$unstructured_env/bin/python" -m nltk.downloader -d "$NLTK_DATA" punkt_tab averaged_perceptron_tagger_eng
```

Обработка выполняется офлайн. В `HF_HOME` нужен полный кэш обеих моделей с `hub/models--*/refs/main`, указывающими на ревизии `7680d6f857780bcf8d49916aa2e8881bd49dee3e` для YOLOX и `f4d4bdc85c3fe4b1fa49658882a5d38bbdd0f343` для Table Transformer. Движок запрашивает `main`. Подготовьте кэш заранее или используйте сохранённый и проверьте контрольные суммы. [YOLOX](https://huggingface.co/unstructuredio/yolo_x_layout/tree/7680d6f857780bcf8d49916aa2e8881bd49dee3e) · [Table Transformer](https://huggingface.co/microsoft/table-transformer-structure-recognition/tree/f4d4bdc85c3fe4b1fa49658882a5d38bbdd0f343) · [Контрольные суммы](</tmp/rag-table-pilots.K5MzkF/unstructured/inputs.sha256>)

Проверенный запуск из корня репозитория. Переменная `corpus` указывает на распакованный комплект участника, а команда `python` запускает штатное окружение MVP.

```bash
pilot=/tmp/rag-table-pilots.K5MzkF/unstructured
(cd "$pilot" && sha256sum -c inputs.sha256)
export RAG_UNSTRUCTURED_PYTHON="$pilot/venv-minimal/bin/python"
export HF_HOME="$pilot/hf" NLTK_DATA="$pilot/nltk_data"
export PATH="$pilot/ocr/usr/bin:$PATH"
export LD_LIBRARY_PATH="$pilot/ocr/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export TESSDATA_PREFIX="$pilot/ocr/usr/share/tesseract-ocr/5/tessdata"

run_dir="$(mktemp -d)/d021"
python -m rag_mvp extract "$corpus/dev/D021.pdf" --page 11 --table 1 --strategy unstructured --out "$run_dir"
python -m rag_mvp serve "$run_dir"
```

CLI сохранил сетку `5 × 4` из 20 ячеек, 10 замечаний и пустой CSV результатов с заголовком. Шесть замечаний открывают отдельные фрагменты шапки для сверки. Чтение со стартом отдельного Python в контрольном запуске заняло 19,42 с по `elapsed_ms` в `run.json`. Числа сверили по исходной странице и `dev_gold` `R001–R009`. Строго совпало 7/9 значений. У `TRUE / Langsmith` получилось `ies`, у `PubmedQA / Langsmith` получилось `0.78 angsmith faithfuln`. В шапке остались `Tangsmith Answer`, `GPT-40` и пропущенные слова, а `PubmedQA` распознан как `PubmedQA|`. Печатное `SQuAD` сохранено. Привязку к ROC-AUC можно открыть из абзаца над таблицей.

Отдельно сверили положение девяти чисел. Во всех 9/9 случаях отмеченный вручную центр попал в рамку соответствующей ячейки. Просмотрщик маркирует её как «Оценочная область ячейки», поскольку рамки пересекают границы наклонённой таблицы. Где рамку сопоставить не удалось, ячейка открывает область таблицы или страницу. `elements.json` хранит сырой вывод Unstructured с HTML и логической сеткой. В `table-transformer-cells.json` лежат OCR-токены, рамки модели и смещения вырезок в пикселях.

Автоматическая сверка повторяет прогон и проверяет числа, привязку к странице и источники замечаний. Она читает gold после извлечения.

```bash
RAG_DEV_CORPUS="$corpus" python -m unittest discover -s tests -p test_unstructured_cli.py -v
```

## Минимальный формат сохранённого прогона

Просмотрщику хватит одного `run.json` на выбранную таблицу, ссылки на исходный PDF и изображения страницы. Сырой выход движка остаётся рядом. Это позволяет открыть прочитанные ячейки, даже если предметный модуль вернул пустой список результатов. [Текущая запись прогона](../../rag_mvp/__main__.py#L57) · [Текущий просмотрщик](../../rag_mvp/viewer/app.js#L59)

| Поля | Что получает просмотрщик |
| --- | --- |
| `strategy`, `versions`, `settings`, `elapsed_ms` | Способ чтения и параметры для повторения запуска. |
| `source` | Имя и SHA-256 исходного PDF, ссылка на PDF, страница с 1, ссылка на превью именно этой страницы. |
| `table` | Метка из подписи, размеры логической сетки, область таблицы, ссылка на фрагмент подписи. Если таблицу не удалось прочитать, `table: null`. |
| `cells[]` | Стабильный в прогоне ID, исходный текст, строка и столбец с 0, охват строк и столбцов, роль при наличии, ссылки на физические фрагменты. Объединённый заголовок хранится одной записью с его охватом. |
| `evidence[]` | ID, страница, текст, прямоугольник `[left, top, right, bottom]` в долях страницы от верхнего левого угла и точность `cell`, `text_fragment`, `estimated_cell`, `table` или `page`. Без доступного прямоугольника передаём `null` и фактическую точность. Здесь же подпись и связанные абзацы. |
| `results[]`, `remarks[]`, `raw_artifacts[]` | Результаты и замечания со ссылками на ID ячеек или фрагментов, а также пути к сырому выходу. У замечания без найденной таблицы остаётся источник на уровне страницы. |

Две статистики из `24.5 (.29)` ссылаются на одну ячейку и один OCR-фрагмент. Два `.586` имеют разные ID ячеек и свои пути по шапке. Для перехода к источнику используются проверенные физические координаты или более широкая доступная область. Индексы строки и столбца нужны для отрисовки сетки. [Точность привязок в спецификации](../../.scratch/source-links/spec.md#как-ставим-привязку-к-источнику)
