# Карта репозитория RAG

## Верхний уровень

| Путь | Назначение и вход |
| --- | --- |
| [`web/`](../web/README.md) | React review-workspace, Hono и TypeScript CLI. |
| [`rag_mvp/`](../rag_mvp/__main__.py) | Рабочий Python-пакет извлечения и воркер RabbitMQ. |
| [`rag_mvp/core/`](../rag_mvp/core/) | Предметные [модели и правила](../rag_mvp/core/domain.py) и [сверка измерений](../rag_mvp/core/measurement_reconciliation.py). |
| [`rag_mvp/strategies/`](../rag_mvp/strategies/) | Ридеры и реестры [текста](../rag_mvp/strategies/article_text_readers.py), [таблиц](../rag_mvp/strategies/table_readers.py) и [графиков](../rag_mvp/strategies/figure_readers.py). |
| `archive/` | Локальный архив исходников и артефактов. |
| [`workers/`](../workers/README.md) | Независимые Rust-пилоты и их контрольные прогоны. |
| [`ops/`](../ops/hype-demo.md) | Запуск Compose и наблюдаемость. |
| [`tests/`](../tests/) | Автоматизированные проверки Python и контрольные входы. |
| [`docs/`](system.md) | Устройство продукта, контракт сдачи и исследования. |
| [`from-chef/`](../from-chef/README.md) | Решения штаба и обратная связь. |
| [`.scratch/`](agents/issue-tracker.md) | Локальный трекер спецификаций и задач. |
| [`runs/`](../runs/README.md) | Команды, контрольные снимки и локальные артефакты прогонов. |
| `output/` | Локальные данные запущенного приложения. |
| [`.agents/`](../AGENTS.md) | Проектные навыки и правила работы агента. |

Входы из корня — [`./rag`](../README.md#cli-прогонов) для CLI и [`compose.yaml`](../compose.yaml) для приложения, воркера и брокера. Отдельный [`compose.rabbitmq.yaml`](../compose.rabbitmq.yaml) запускает брокер для разработки на хосте. [`Dockerfile`](../Dockerfile) собирает образ, `requirements*.txt` описывают Python-зависимости. pnpm обслуживает [пакет `web`](../web/package.json).

## Настройки запуска

- Для каждого запуска используем корневой `.env` на основе [`.env.example`](../.env.example). Полный Compose требует `RAG_CORPUS_DIR`. Параметры брокера и локальных процессов приведены в примере. При запуске на хосте переменные получает каждый нужный процесс через своё окружение. На сервере применяем те же имена переменных.
- Python-воркер получает адрес уже запущенного Ollama через `RAG_OLLAMA_URL`. Способ запуска, доступные устройства и параллельность относятся к окружению Ollama. [Настройка и проверка подключения](../ops/ollama.md)
- Обычный запуск работает на CPU. Настроенный Ollama делает модельные ридеры доступными для явного выбора. План `auto` остаётся прежним. Если выбранная модель недоступна, попытка сообщает причину, а остальные этапы продолжают работу.
- Для сравнения моделей указываем в отчёте фактическое размещение модели и настройки запуска Ollama.

## Приёмка раскладки

1. [Архив TypeScript](../.scratch/repository-layout/issues/01-archive-typescript-domain.md) сохраняет свои тесты и ссылки на рабочее ядро.
2. [Перенос Python-ядра](../.scratch/repository-layout/issues/02-organize-python-core.md) проверен предметными тестами и повтором D001/Table 1 по сохранённой сетке и тексту.
3. [Перенос стратегий](../.scratch/repository-layout/issues/03-organize-strategy-registries.md) проверен сравнением каталога CLI и HTTP, сборкой Compose и повтором D001.
4. [Адрес Ollama](../.scratch/repository-layout/issues/04-configure-ollama-url.md) проверен из Compose с заглушкой, закрытым адресом и на обычном плане `auto`.
