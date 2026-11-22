# Архитектурные решения (ADR)

Журнал ключевых решений по устройству пайплайна извлечения, предметного ядра, выпуска CSV и границ применения LLM/VLM.

| ADR | Решение | Статус |
| --- | --- | --- |
| [0001](0001-pipeline-postgres-and-rabbitmq.md) | Единая БД состояния пайплайна (Postgres) и отдельная доставка заданий (RabbitMQ) | Принято (переход в roadmap) |
| [0002](0002-observation-assertion-assemble-core.md) | Ядро-конструктор `Observation → Assertion → assemble()` и реестр стратегий | Внедрено |
| [0003](0003-two-tier-release-and-ocr-verification.md) | Двухуровневый выпуск (`Strict/Partial` + `Entity Passport B3`) и допуск OCR-сканов | Внедрено |
| [0004](0004-llm-and-vlm-integration-boundary.md) | Граница LLM/VLM и подключение сильной LLM/RAG поверх `assemble()` | Принято (4B отключена; LLM-плагин в roadmap) |

Подробное руководство по добавлению новых ридеров, правил и LLM-стратегий — в [Расширении и Roadmap](../extensibility-and-roadmap.md).
