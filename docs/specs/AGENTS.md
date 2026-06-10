cat > AGENTS.md <<'EOF'
# AGENTS.md

## Контекст проекта

Этот репозиторий является fork OpenStack Masakari.

Целевая ветка:
- stable/2025.1
- OpenStack Epoxy 2025.1

Цель доработки:
реализовать поэтапное восстановление ВМ после отказа гипервизора средствами Masakari, без Mistral.

Основной документ с ТЗ:
- docs/specs/CODEX_MASAKARI_STAGED_RECOVERY_ETCD.md

## Требуемая архитектура

Нужно реализовать staged recovery для host failure workflow Masakari:

1. Использовать кастомные Masakari TaskFlow task-и.
2. Не использовать Mistral.
3. Не менять поведение Masakari для существующих recovery workflow, если staged recovery явно не подключён.
4. Для эвакуации ВМ использовать Nova Compute API microversion 2.95+.
5. Эвакуированная ВМ должна оставаться выключенной на целевом compute-host.
6. После эвакуации ВМ должны запускаться поэтапно.
7. Должен быть лимит одновременно стартующих ВМ на один destination hypervisor.
8. Состояние staged recovery нужно хранить в etcd.
9. Лимиты запуска нужно реализовать через etcd TTL leases.
10. Решение должно работать при нескольких masakari-engine.
11. Решение должно быть idempotent и retry-safe.
12. Нельзя использовать in-memory locks для HA-critical логики.
13. Start slot должен оставаться занятым до перехода ВМ в ACTIVE, ERROR или до timeout.
14. ВМ, которые до сбоя были SHUTOFF, не должны стартовать автоматически.

## Правила реализации

- Делай изменения небольшими, пригодными для code review.
- Не переписывай несвязанный код Masakari.
- Не меняй глобальное значение NOVA_API_VERSION для существующих вызовов Masakari.
- Добавь отдельный helper для Nova evacuation с microversion 2.95.
- Все новые config options должны иметь разумные значения по умолчанию.
- Все новые config options должны быть задокументированы.
- etcd operations должны быть безопасны при retry.
- При ошибках workflow должен оставлять состояние, пригодное для повторного запуска.
- Добавь unit-тесты для:
  - Nova microversion 2.95;
  - etcd state store;
  - etcd lease/limiter;
  - idempotency;
  - TaskFlow task behavior;
  - сценария нескольких masakari-engine.

## Предпочтительный порядок работы

1. Сначала изучи текущий код Masakari.
2. Сначала верни план, не меняя код.
3. Реализуй по одной фазе за раз.
4. После каждой фазы покажи список изменённых файлов и краткое объяснение.
5. Не делай git push без явной команды.
6. Не используй опасные режимы с полным доступом.
EOF