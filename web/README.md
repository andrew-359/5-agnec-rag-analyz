# Веб RAG

Основной интерфейс — React в `src/review-workspace`. Hono выдаёт один и тот же
веб на `/`, браузер обращается к API на том же адресе. Ссылки с `?lab=1`
также открываются. Опубликованные версии и PDF-фрагменты по-прежнему доступны
по адресам `/publications/…`.

## Запуск

В Docker Compose откройте порт веба после запуска через [`compose.yaml`](../compose.yaml) ([инструкция в корневом README](../README.md#собрать-и-запустить-в-docker)).
Для разработки на хосте установите зависимости из корня проекта:

```bash
pnpm --dir web install --frozen-lockfile
pnpm --dir web dev
```

Откройте [Vite на 4311](http://127.0.0.1:4311/). Hono по умолчанию слушает
`127.0.0.1:4310`. Для штатной сборки запустите `./rag demo` из корня и откройте
[4310](http://127.0.0.1:4310/).

Для обработки новых PDF нужны [PDF-зависимости, RabbitMQ и воркер](#разобрать-статью-через-rabbitmq).
У Hono и воркера должны совпадать хранилище, адрес брокера и очередь. Запустите
из корня проекта `python -m rag_mvp.article_worker` в том же окружении.
Из «Проверок» после открытия источника можно выделить область графика и
запустить точечный разбор.

## Разобрать статью через RabbitMQ

Установите зависимости проекта из `requirements.txt` и зависимости чекера из `tools/requirements.txt` [комплекта участника](../docs/context/viniti.md) в Python-окружение, которое использует Hono. По умолчанию чекер берётся из распакованного комплекта в `docs/context/`. Если комплект расположен в другом месте, передайте Hono абсолютный путь к `tools/check_submission.py` через `RAG_SUBMISSION_CHECKER`. Из корня репозитория поднимите локальный брокер или укажите URL существующего.

```bash
export RAG_RABBITMQ_PASSWORD="$(openssl rand -hex 16)"
docker compose -f compose.rabbitmq.yaml up -d --wait
export RAG_AMQP_URL="amqp://rag:${RAG_RABBITMQ_PASSWORD}@127.0.0.1:5672/%2F"
```

Пароль задаётся при создании тома RabbitMQ. Для последующих запусков с тем же томом используйте прежнее значение. Если задан `RAG_RABBITMQ_PORT`, используйте его и в URL.

С тем же `RAG_AMQP_URL` в окружении запустите сервер командой `pnpm dev` из `web/` и исполнителя командой `python -m rag_mvp.article_worker` из корня репозитория. Для параллельной обработки на хосте можно запустить несколько экземпляров `python -m rag_mvp.article_worker` с теми же `RAG_AMQP_URL`, `RAG_AMQP_QUEUE` и `RAG_ARTICLE_STORE` (рекомендуется ограничить потоки через `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1`); в Docker Compose число реплик задаётся переменной `RAG_WORKERS`. Оба процесса используют `RAG_AMQP_QUEUE` (по умолчанию `rag.article-extraction`) и общий каталог `output/web-articles/`. Для изолированного запуска задайте обоим процессам один абсолютный путь в `RAG_ARTICLE_STORE` и отдельное имя очереди. Hono хранит версии в `output/publications/`. Этот каталог задаётся через `RAG_PUBLICATION_STORE`.

В браузере нажмите «Разобрать статью» и загрузите PDF. После «Авто» откроется
карточка прогона. В «Результатах» можно открыть ячейку в PDF, во вкладке
«Проверки» — причины пропусков, из «Паспорта и выгрузки» — ZIP с семью CSV.

## Пробный перевод краткого обзора

На хосте настройте модель Helsinki-NLP OPUS-MT en→ru и CTranslate2 INT8
в Python-окружении, которое указывает `RAG_PYTHON`.

```bash
python -m pip install -r requirements-translation.txt
stage=$(mktemp -d)
model="$HOME/.cache/rag/opus-en-ru-2020-02-11-int8"
curl -fL https://object.pouta.csc.fi/OPUS-MT-models/en-ru/opus-2020-02-11.zip -o "$stage/opus.zip"
echo "798027c7e4ae7ddf89fea13ce80de517b6726d7e710fa5a9b5a376316dbf1677  $stage/opus.zip" | sha256sum --check
unzip -q "$stage/opus.zip" -d "$stage/original"
mkdir -p "$(dirname "$model")"
ct2-opus-mt-converter --model_dir "$stage/original" --output_dir "$model" --quantization int8
cp "$stage/original/"{source.spm,target.spm,LICENSE} "$model/"
```

Если модель лежит в другом месте, задайте `RAG_OVERVIEW_TRANSLATION_MODEL`.
После настройки перезапустите Hono. Он проверяет модель при старте и показывает
кнопку в обзоре статьи с извлечённой аннотацией или выводом. В базовый
Docker-образ модель и её зависимости не включены.

## Проверки

```bash
pnpm --dir web typecheck
pnpm --dir web test:qa
pnpm --dir web test:e2e
```

[Java/Selenium](../qa/java/README.md) проверяет ту же клиентскую сборку.
Исходники прежнего React-веба и статических макетов находятся в локальном
`archive/web-legacy/`. API опубликованных графиков и сохранённых прогонов
остаются доступны серверным потребителям.
