# Одна точка графика: воспроизведение публикации

21 ноября 2026. Реализация в `rag_mvp/plot_publish.py`; границы — в [спецификации](../../.scratch/plot-publication/spec.md). Нужны Python-окружение MVP, Node ≥24, зависимости веба и локальный комплект участника. Веб-просмотр технический; Living UI этим срезом не перерабатывается.

Из корня checkout с активированным Python-окружением:

```bash
python -m pip install -r requirements-checker.txt
corpus="$PWD/docs/context/RAG_ВИНИТИ_участникам_2026-11-20"
python -m rag_mvp.plot_extract "$corpus/dev/D001.pdf" --page 8 --figure 3 --panel a --series HotpotQA --strategy vector-line --x-axis anchors --y-axis EM --out /tmp/d001-figure3.json
python -m rag_mvp.plot_publish --plot /tmp/d001-figure3.json --context .scratch/plot-publication/d001-figure3-context.json --pdf "$corpus/dev/D001.pdf" --template "$corpus/submission_template" --out runs/published-plots
node web/scripts/serve-published-plots.ts
```

Если корпус расположен в другом checkout, задайте его реальный путь. CLI выводит папку `runs/published-plots/plot-<hash>`. Открыть `http://127.0.0.1:4313/published/plot-<hash>/index.html#point-1`. Порт меняется через `PORT`, каталог публикаций — через `RAG_PUBLISHED_PLOTS`. Основной Hono-сервер подключает тот же маршрут и по умолчанию читает этот каталог.

Повторная публикация в тот же каталог с теми же входами запрещена. Для нового независимого прогона можно выбрать другой `--out`. Версия зависит от хешей JSON наблюдений, проверенного контекста, схемы CSV, checker, кода публикации и preview. SHA-256 PDF проверяется во всех входах и после копирования. Контрольные суммы не заменяют подпись или права файловой системы; они обнаруживают изменение файла при неизменённом manifest.

## Содержимое

- `run.json` — общий `domain.Result`, ID R1, погрешность, параметр X, источники полей и координаты точки; `kind=published-plot`, частичное покрытие. Ссылки PDF закреплены за версией.
- Семь CSV с точными заголовками `submission_template/schema.json`; R1 связан с E1/C1/DS1/T1. Настройки anchors и погрешность записаны в protocol/note. `raw_value=~0.528` отличает геометрическое чтение от напечатанного числа. Нормализованное значение сохраняет исходную единицу fraction.
- `issues.csv` — замечание о приблизительном чтении; оно не препятствует экспорту проверенной точки.
- `plot-input.json`, `reviewed-context.json` — неизменённые входы; исходные оси, калибровка, серия и команда не потеряны. Проверенный вручную контекст содержит цитаты и страницы. Совпадение цитаты не доказывает применимость условия автоматически.
- `source.pdf`, `index.html`, `PASSPORT.md`, `schema.json`, `check-report.json`, `manifest.json` — исходник, просмотр, описание и проверка версии.

Публикация допускает exit 2 checker с явно видимым статусом «требует внимания»; exit 1 прерывает её до появления финальной папки. Отсутствие PDF-библиотеки checker также остаётся предупреждением, а не превращается в успешную проверку цитат. В принятом прогоне установлен `pypdf==7.1.1`, checker завершился с exit 0. Семантическая полнота всего PDF не сертифицируется.

## Проверки

```bash
RAG_DEV_CORPUS="$corpus" python -m unittest discover -s tests -p 'test_plot*.py' -v
python -m unittest discover -s tests -p 'test_domain.py'
node --test web/tests/plot-publications.test.ts
```

[Фактическая приёмка и ограничения](../../.scratch/plot-publication/issues/01-d001-point.md). Автоматическая очередь публикаций остаётся отдельной интеграцией; текущий пакет не содержит другие таблицы или точки D001.

## Объединённый веб

React-каталог и просмотр версии теперь подключены. Из `web/` установите зависимости через `pnpm install --frozen-lockfile`, выполните `pnpm prepare:demo --pdf /path/to/dev/D001.pdf`, затем `pnpm build`. Запуск: `RAG_PYTHON=/path/to/.venv/bin/python PORT=4310 pnpm start`.

Открыть `http://127.0.0.1:4310/plots`: опубликованный график показывается в основном интерфейсе с PDF.js, условиями, допуском, паспортом, отчётом и выгрузкой. Каталог открывается из шапки загрузки/карты. На карте D001 выберите рисунки и «Разобрать график» у Figure 3. После разбора доступна ссылка на опубликованный результат того же PDF и панели. Для чужого/неподтверждённого графика такая ссылка не появляется.

Сервер использует `RAG_PUBLISHED_PLOTS` или `runs/published-plots`. Существующие карты можно подключить через `RAG_ARTICLE_STORE`; в ручной проверке их копия помещена в локальное хранилище этой ветки. Для нового разбора таблиц по-прежнему нужны действующие RabbitMQ и Python-исполнитель; разбор графика вызывается через отдельный API без RabbitMQ. Публикация нового пакета по-прежнему выполняется CLI. Living UI остаётся отдельным прототипом.

[Приёмка объединения с вебом](../../.scratch/plot-publication/issues/02-unify-web.md).
