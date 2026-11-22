# RAG · кейс ВИНИТИ

Извлекает результаты экспериментов из научных PDF о Retrieval-Augmented Generation. В вебе можно проверить число и его условия по источнику, сравнить результаты и скачать выгрузку в семи CSV.

## Запустить веб RAG

Понадобятся Node.js 24 и pnpm 10. Запускайте из корня проекта.

```bash
pnpm --dir web install --frozen-lockfile
./rag demo
```

Откройте [localhost:4310](http://127.0.0.1:4310/). Для разработки с горячей перезагрузкой используйте `pnpm --dir web dev` и [localhost:4311](http://127.0.0.1:4311/). Для обработки новых PDF нужны RabbitMQ и Python-воркер. [Настройка локальной обработки](web/README.md#разобрать-статью-через-rabbitmq)

## CLI прогонов

Запускайте из корня проекта после старта сервера.

```bash
./rag strategies
./rag run ./article.pdf --out runs/article
```

Первая команда показывает доступные ридеры, вторая запускает разбор статьи. Для `run` сервер должен быть подключён к очереди и воркеру. Флаг `--base-url` меняет адрес сервера, по умолчанию `http://127.0.0.1:4310`. [Сравнение ридеров и разбор отдельной таблицы](docs/cli.md)

## Собрать и запустить в Docker

Укажите абсолютный путь к распакованному комплекту ВИНИТИ. Compose соберёт приложение и запустит веб, RabbitMQ и воркер.

```bash
export RAG_CORPUS_DIR=/absolute/path/to/viniti-corpus
docker compose up --build --wait
curl -fsS http://127.0.0.1:4310/readyz
```

Откройте [localhost:4310](http://127.0.0.1:4310/) и выберите **«Разобрать статью»**. [Комплект участника](docs/context/viniti.md) · [Подробности Docker Compose](ops/hype-demo.md)

## Результаты

**84 из 89** статей в предметной области дали **11 148 записей**. P020 дала 0 записей. На 21 статье `dev_gold` точность составила 89,23%, полнота — 33,23%.

[Паспорт данных](docs/data-passport.md) · [Формат CSV](docs/submission-contract.md) · [Карта репозитория](docs/repository-map.md) · [QA / Allure](qa/java/README.md)
