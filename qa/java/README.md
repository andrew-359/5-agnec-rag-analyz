# Автотесты React-лаборатории

Сценарии на Java 21, Gradle, JUnit 5, Selenium и Allure. Gradle собирает приложение через `pnpm build:client`. Каждый тест запускает собранный React на локальном случайном порту и подаёт синтетические ответы API. На машине нужны JDK 21, Node.js и pnpm из [веб-проекта](../../web/package.json), Chrome и совместимый ChromeDriver. Selenium Manager подбирает браузер и драйвер по умолчанию. Для явно установленных бинарников укажите абсолютные пути в `CHROME_BIN` и `CHROMEDRIVER`.

## Запуск из корня репозитория

```bash
pnpm --dir web install --frozen-lockfile
qa/java/gradlew -p qa/java test
```

Набор проверяет библиотеку, поиск и фильтры, карту, PDF с восстановлением источника, повтор после 503, черновики, сравнение и паспорт выбранной версии. [Карта покрытия по всем 130 ID каталога](COVERAGE.md).

### Проверки без браузера

Из корня репозитория, после установки зависимостей web:

```bash
python3 -m venv /tmp/rag-qa-python
/tmp/rag-qa-python/bin/python -m pip install -r qa/requirements-ci.txt
RAG_PYTHON=/tmp/rag-qa-python/bin/python pnpm --dir web test:qa
RAG_PYTHON=/tmp/rag-qa-python/bin/python bash qa/run_workers.sh
```

Тот же набор запускается в CI. Node проверяет реальную загрузку PDF, работу API, SQLite, сравнения и экспорт. Python проверяет правила обработки, привязку, отрицательные случаи, работу с OCR-сетками, графики и диагностику прогона. Для тестов с оригиналами и брокером см. [каталог контрольных прогонов](../../runs/README.md#один-рисунок-с-явным-ридером-через-cli).

### Проверки на оригиналах и RabbitMQ

Из корня задайте абсолютный путь к комплекту участника и Python из окружения воркера. Для проверки очереди нужен отдельный тестовый RabbitMQ. Тесты создают временные очереди и хранилища.

```bash
export RAG_DEV_CORPUS=/absolute/path/to/participant-kit
export RAG_PYTHON=/absolute/path/to/venv/bin/python
PYTHONPATH=tests "$RAG_PYTHON" -m unittest -v \
  test_local_condition_cli test_figure_label_search test_plot_extract.PlotPilotTest \
  test_prose_results test_prose_failure_counts

D001_PDF="$RAG_DEV_CORPUS/dev/D001.pdf" RAG_AMQP_URL=amqp://localhost:YOUR_TEST_PORT \
  pnpm --dir web exec node --test tests/figure-cli.test.ts
```

Для локального контрольного прогона D005 и P003 нужны ридеры из рабочего окружения Python. RabbitMQ передаёт задания настоящим воркерам, ответ Ollama в проверке отказов подставляет локальная фикстура.

## Allure

Gradle собирает Allure после тестов и сохраняет их исходный статус. HTML находится в `qa/java/build/allure-report/`, сырые данные в `qa/java/build/allure-results/`, JUnit XML в `qa/java/build/test-results/test/`. Задача `allureReport` отдельно пересобирает HTML из текущих сырых данных.

В разделе **Behaviors** браузерные проверки сгруппированы по функции и типу данных: синтетические или живой стенд. В нагрузочном отчёте **Behaviors** и **Suites** разделяют одиночный прогон и пересекающиеся запуски D001. Вкладка **Categories** показывает категории дефектов. Серверные и Python-проверки отображаются отдельно в сводке GitHub Actions.

Чтобы открыть отчёт, запустите HTTP-сервер из корня и перейдите на [локальный Allure](http://127.0.0.1:8080/).

```bash
python3 -m http.server 8080 --bind 127.0.0.1 --directory qa/java/build/allure-report
```

## Приёмка сохранённых выпусков через реальный Hono

Из корня репозитория, после установки зависимостей:

```bash
python3 qa/java/run_live.py d001 runs/d001-web-acceptance.4M2HG1
python3 qa/java/run_live.py d013 runs/react-acceptance-zqbln2v8
python3 qa/java/run_live.py p056 runs/react-acceptance-t0__75kf
```

Чтобы проверить ссылку на архивный D001 при наличии более свежего выпуска, передайте его каталог:

```bash
python3 qa/java/run_live.py d001 runs/d001-web-acceptance.4M2HG1 \
  --newer-d001 runs/react-acceptance-t0__75kf/publications/publication-7fcbb21b0bdf5bb222b347d0b5e8d6587aa8af5c5f855f62322d0d7ad5427d3c/version-408d65f1d974e27991dbff50563f6a67521ad819
```

Команда поднимает React и Hono на свободных портах и делает отдельную копию SQLite для сервера. Она читает сохранённые выпуски с реальными PDF и ZIP. Для запуска нужны локальные каталоги указанных прогонов. Повторная загрузка и распознавание выполняются в отдельных проверках с RabbitMQ.

Отчёты Allure и журналы Hono и React лежат в `qa/java/build/d001/`, `qa/java/build/d013/` и `qa/java/build/p056/`. Быстрый запуск Gradle и GitHub Actions используют синтетические данные, а оригиналы остаются локально. [Ручная сверка D001](../../runs/d001-web-acceptance.4M2HG1/REPORT.md) · [Контроль D013 и P056](../../runs/react-acceptance-t0__75kf/REPORT.md)

## Проверка диагностики

```bash
qa/java/gradlew -p qa/java test --tests '*restoresExactSourceAfterReload' \
  -PqaFailSource=true -PqaOutput=build/diagnostic
```

Фикстура возвращает `503` только для PDF закреплённого выпуска. Команда ожидаемо завершится ненулевым кодом. Отчёт в `qa/java/build/diagnostic/allure-report/` показывает ошибку и вложения со скриншотом, URL, участком DOM, консолью Chrome, запросами стенда и журналом ChromeDriver. Переопределение каталога сохраняет основной отчёт.

`synthetic.pdf` и `publication.json` созданы для этого теста. Две страницы PDF моделируют два результата Alpha `.422` с разными адресами. Остальные строки моделируют несовместимые метрики, ноль, неопределённый генератор и несколько записей из одного фрагмента. Для запуска не требуется комплект ВИНИТИ, RabbitMQ и запущенная лаборатория на 4313. [План следующего этапа QA](../../.scratch/qa-automation/spec.md)

## Приёмка RAG 0.99 на живом стенде

`LiveLabAcceptanceTest` работает на запущенной React-лаборатории. Подготовьте штатным Auto сохранённые D001, D013 и P079, укажите адрес лаборатории и путь к оригинальному комплекту участника. Передайте `RAG_QA_D001_ARTICLE_ID` и `RAG_QA_D001_RUN_ID` из карточки, аналогично `RAG_QA_D013_*` и `RAG_QA_P079_*`. Каждый сценарий включается своим ID прогона.

Для ручной рамки добавьте `RAG_QA_D001_CAPTION_RUN_ID`. Это ID отдельного прогона Figure 3 без выпуска, у которого замечание открывает подпись на физической странице 8. Воспроизводимый контроль создаёт штатный [CLI одного рисунка](../../runs/README.md#один-рисунок-с-явным-ридером-через-cli) с явным `vector-bar` на оригинальном D001. Selenium выделяет две панели, уточняет рамку до Figure 3(a), ждёт итог `auto` и явно применяет его. Запускайте этот сценарий с изолированными очередью и хранилищем, где можно создавать новые прогоны и выпуск.

Для проверки технического повтора задайте `RAG_QA_D001_FAILED_RUN_ID` на отдельном контуре с завершившимся ошибкой прогоном корректной области D001. К моменту запуска теста обработчик должен быть работоспособен, чтобы повтор штатно прочитал сохранённую рамку.

```bash
export RAG_QA_ORIGIN=http://127.0.0.1:4314
export RAG_QA_CORPUS=/absolute/path/to/participant-kit
./qa/java/gradlew -p qa/java test --tests '*LiveLabAcceptanceTest' \
  -PqaOutput=../../runs/rag-099-acceptance/selenium-live
```

## GitHub Actions

[React QA](../../.github/workflows/qa.yml) запускает Selenium, серверные проверки и быстрый набор Python на каждый push и вручную. В **Actions → React QA → нужный прогон** сводка показывает схему пайплайна, статусы и счётчики каждого набора, коммит и ссылку на архив. В архиве находятся два автономных HTML-отчёта, обычный отчёт Selenium, сырые результаты, JUnit XML, логи и диагностика. Срок хранения — 14 дней. Нагрузочный отчёт строится из сохранённого локального замера D001 от 22 ноября. Свежий нагрузочный замер выполняется отдельно. [Исходные метрики для CI](load-baselines/d001-2026-11-22.json).

Для контрольного падения в **Run workflow** включите `fail_pdf_source`. GitHub отметит прогон красным и сохранит отчёт. При сбое до запуска браузера в архиве будут только доступные логи и данные запуска.

### Приёмка в текущем репозитории

1. Откройте [React QA в andrew-359/RAG](https://github.com/andrew-359/RAG/actions/workflows/qa.yml). Выберите нужный коммит или запустите **Run workflow** на `main` с выключенным `fail_pdf_source`. Ожидаются зелёный статус и все тесты зелёные. В сводке отдельно видны статусы серверных и Python-проверок. Allure показывает Selenium-сценарии.
2. Скачайте `allure-<run_id>-<attempt>` в **Artifacts** или по ссылке из **Summary**. Распакуйте архив и откройте в браузере `open-in-browser/browser/index.html` для Selenium или `open-in-browser/load/index.html` для нагрузочного замера. Категории сценариев видны в **Behaviors**.
3. Запустите `main` с включённым `fail_pdf_source`. Ожидается одно падение. В артефакте упавшего прогона раскройте сценарий восстановления источника и проверьте скриншот, URL и запрос PDF с ответом `503`.

После приёмки переносим workflow и модуль QA в итоговый репозиторий. Если структура `web/` и `qa/java/` сохранится, конфигурацию можно перенести целиком. При другой структуре сверяются пути к сборке и отчётам в Gradle и workflow. На стороне GitHub проверяются доступность Actions, ветка по умолчанию и разрешённые actions.
