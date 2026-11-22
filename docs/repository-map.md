# Карта репозитория RAG

## Верхний уровень

| Путь | Назначение и вход |
| --- | --- |
| [`web/`](../web/README.md) | React review-workspace, Hono API и TypeScript CLI. |
| [`rag_mvp/`](../rag_mvp/__main__.py) | Рабочий Python-пакет извлечения, аудит, сборщик CSV и воркер RabbitMQ. |
| [`rag_mvp/core/`](../rag_mvp/core/) | Предметные [модели и правила](../rag_mvp/core/domain.py) и [сверка измерений](../rag_mvp/core/measurement_reconciliation.py). |
| [`rag_mvp/strategies/`](../rag_mvp/strategies/) | Ридеры и реестры [текста](../rag_mvp/strategies/article_text_readers.py), [таблиц](../rag_mvp/strategies/table_readers.py) и [графиков](../rag_mvp/strategies/figure_readers.py). |
| [`experiments/entity-passport/`](../experiments/entity-passport/extract.py) | Проверенный добавочный слой извлечения табличных значений и сборки CSV. |
| [`tests/`](../tests/) | Автоматизированные проверки Python и контрольные входы. |
| [`qa/`](../qa/java/README.md) | Сквозные проверки воркера ([`qa/run_workers.sh`](../qa/run_workers.sh)) и автотесты Java / Allure. |
| [`scripts/`](../scripts/export_clean_repo.sh) | Служебные скрипты выпуска и экспорта чистого снимка репозитория. |
| [`runs/`](../runs/full_pdf_batch.py) | Скрипты пакетного прогона, сверки сохранённых выходов и контрольные снимки. |
| [`docs/`](system.md) | Устройство продукта, [паспорт данных](data-passport.md), [контракт CSV](submission-contract.md), [CLI](cli.md), [ADR](adr/README.md) и [roadmap](extensibility-and-roadmap.md). |
| [`.github/`](../.github/workflows/qa.yml) | Пайплайн CI-проверок (web QA, Python worker, Java/Allure). |

Входы из корня — [`./rag`](../README.md#cli-прогонов) для CLI и [`compose.yaml`](../compose.yaml) для веб-приложения, воркера и брокера RabbitMQ. [`Dockerfile`](../Dockerfile) собирает образ, `requirements*.txt` описывают Python-зависимости, pnpm обслуживает [пакет `web`](../web/package.json).

## Настройки запуска

- Для запуска используется корневой `.env` на основе [`.env.example`](../.env.example). Полный Compose требует `RAG_CORPUS_DIR` (путь к распакованному комплекту PDF). При запуске на хосте переменные передаются каждому процессу через окружение; на сервере используются те же имена переменных.
- Python-воркер получает адрес уже запущенного Ollama через `RAG_OLLAMA_URL` (по умолчанию `http://127.0.0.1:11434` при локальном запуске). Подробности параметров запуска приведены в [документации CLI](cli.md#локальный-qwen).
- Штатный план `auto` работает на CPU без внешних моделей. Настроенный Ollama делает модельные ридеры (`qwen-vision`, `propose-table-conditions`) доступными для явного выбора. Если выбранная модель недоступна, попытка сообщает причину, а остальные этапы продолжают работу.
- Для воспроизводимого сравнения моделей в отчёте фиксируются фактическое размещение модели и параметры запуска Ollama.

## Контроль архитектуры и границ модулей

1. **Единое Python-ядро (`rag_mvp/core/`)** — доменные типы `Observation`, `ConditionAssertion`, правила валидации метрик/датасетов и `reconcile_measurements` покрыты модульными тестами в [`tests/`](../tests/).
2. **Расширяемые реестры стратегий (`rag_mvp/strategies/`)** — ридеры текста, таблиц и графиков подключаются через единый контракт и отображаются в `./rag strategies` и `GET /api/strategies` (подробнее в [руководстве по расширению](extensibility-and-roadmap.md)).
3. **Изоляция LLM/VLM и двухслойный выпуск** — границы доверия и верификации зафиксированы в [ADR-0002](adr/0002-observation-assertion-assemble-core.md), [ADR-0003](adr/0003-two-tier-release-and-ocr-verification.md) и [ADR-0004](adr/0004-llm-and-vlm-integration-boundary.md).
