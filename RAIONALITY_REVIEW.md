# Рациональность кода: openrot

**4 параллельных агента | Проверка каждого блока на необходимость**

---

## СУММАРНАЯ ТАБЛИЦА

| Категория | Найдено | Самое критичное |
|-----------|---------|-----------------|
| Мёртвый код (dead code) | 10 | `node_from_records` — никогда не вызывается |
| Over-engineering | 40+ | 6 Pydantic-моделей для простых данных, 8 обёрточных функций |
| Дублирование | 25+ | `cfg.load/save` в 10+ местах, 8+ `httpx.Client`, 3 раза `HEALTH_URL` |
| Ненужные фичи / непротестированные | 10 | `self_update_cmd`, `config_edit` — без тестов |
| **ИТОГО** | **~85+** | |

---

## 1. МЁРТВЫЙ КОД И НЕИСПОЛЬЗУЕМЫЕ КОМПОНЕНТЫ

### Критические

**1. `node_from_records()` — никогда не вызывается в продакшене**
- `core/nodes.py:28-39` — определена, но не используется нигде кроме тестов
- Продакшен-код использует `verify.nodes_from_vless_survivors()` и `verify.nodes_from_proxy_survivors()` вместо неё
- **Действие**: Удалить или сделать `_` приватной функцией

**2. `check_for_update()` всегда возвращает `updated=False`**
- `self_update.py:146-151` — возвращает `UpdateResult(updated=False)` даже когда `latest > current`
- CLI в `cli.py:835` проверяет `result.updated`, но оно всегда `False` — обновление никогда не запускается через эту ветку
- **Действие**: Исправить логику: `updated=(latest > current)`

**3. `TypeVar` в `verify.py` — мёртвый импорт**
- `verify.py:22,40` — `T = TypeVar("T")` импортируется и определяется, но никогда не используется (PEP 695 generic syntax `[T]` создаёт новый scope)
- **Действие**: Удалить `from typing import TypeVar` и `T = TypeVar("T")`

### Умеренные

**4. Пустые `__init__.py`** — `core/__init__.py`, `providers/__init__.py` (0 байт)
**5. `# type: ignore[attr-defined]` в `bridge.py:114`** — маскирует несуществующую ошибку
**6. `profile_id` в `models/__init__.py:18`** — экспортируется но не импортируется продакшен-кодом
**7. `openrot provider` команда** — вызывает `opencode.toggle()`; `opencode.py` импортируется только из `cli.py`, нет тестов

### Мелкие

**8. Закомментированные enum'ы** — `models/enums.py:17` — `# follow-up: HYSTERIA2, VMESS, SS`
**9. Непроверенные CLI-команды** — `config_edit`, `self_update_cmd`, `warp install` (реальный путь)

---

## 2. OVER-ENGINEERING: КОГДА КОД СЛОЖНЕЕ ЗАДАЧИ

### Pydantic-модели, где достаточно dict/ничего

| # | Класс | Файл | Почему избыточен |
|---|-------|------|-----------------|
| 1 | `LogOptions` | `singbox.py:74` | Одно поле `level: str = "warn"`. Pydantic- overhead за один string |
| 2 | `RealityTLSOptions` | `singbox.py:20` | 2-3 поля, ни одной валидации |
| 3 | `UTLSOptions` | `singbox.py:28` | 2 поля, `type` всегда `"utls"` |
| 4 | `TransportOptions` | `singbox.py:35` | `type` всегда `"ws"`, ничего не валидирует |
| 5 | `MixedInbound` | `singbox.py:65` | Hardcoded `type` и `tag`, всегда одно и то же |
| 6 | `ServiceState` / `UpdateResult` | `self_update.py:26,67` | NamedTuple сразу распаковывается |

**Исправление**: Заменить на dict-литералы или `dataclass`. Pydantic оправдан только для `Config`, `Node`, `Profile`.

### Обёрточные функции (делают ровно то же)

| # | Функция | Файл | Просто вызывает |
|---|---------|------|---------------|
| 1 | `start_proxy` / `start_free_proxy` | `proxy.py:35-44` | `_launch(config_gen, bin)` |
| 2 | `_log` | `bridge.py:45-47` | `events.info(msg)` |
| 3 | `_running_level` | `bridge.py:205-206` | `cfg.active_level != NONE and check(cfg)` |
| 4 | `refresh_profile` | `refresh.py:19-29` | `fetch_profile_nodes()` |
| 5 | `_progress_reports` | `cli.py:698-719` | Два closure, отличающиеся одним полем |
| 6 | `save_pid` / `load_pid` | `proxy.py:83-95` | `_write_pid` / чтение с разным path |
| 7 | `save_daemon_pid` / `load_daemon_pid` | `proxy.py:98-110` | То же, что и выше |

**Исправление**: Удалить обёртки, вызвать оригинал напрямую.

### Абстракции, добавляющие ничего

| # | Что | Файл | Почему не нужно |
|---|-----|------|---------------|
| 1 | `Bridge(ThreadingHTTPServer)` | `bridge.py:386` | Наследует только чтобы передать `BridgeHandler` в `__init__` |
| 2 | `UpstreamError(Exception)` | `bridge.py:60` | Used once; `httpx.HTTPError` suffice |
| 3 | `Stage` / `ProgressFn` type aliases | `verify.py:36-37` | Используются < 5 раз; не улучшают читаемость |
| 4 | `update_config[T]` generic | `config.py:201` | Generic `[T]` добавляет сложность без benefit |
| 5 | `_running_level` callback | `bridge.py:205-206` | Called once |

**Исправление**: Снять наследование, удалить исключение, использовать inline-типы.

### Enum overuse

| # | Enum | Файл | Почему не нужен |
|---|------|------|---------------|
| 1 | `WarpStatus` | `warp.py:24-32` | `.value` везде; string constants эквивалентны |
| 2 | `ActiveLevel` | `models/enums.py` | `if cfg.active_level == ActiveLevel.WARP` ≡ `== "warp"` |
| 3 | `NodeProtocol` | `models/enums.py` | Используется в `if protocol in (HTTP, SOCKS5)` — string check works |

**Исправление**: String constants. Enums не дают валидации (значения приходят из YAML).

### Premature optimization

| # | Что | Файл | Почему не нужно |
|---|-----|------|---------------|
| 1 | Double-checked locking for `httpx.Client` | `bridge.py:157-195` | CLI tool, не high-concurrency сервер. Module-level init thread-safe |
| 2 | `_dedupe_by_ip` complex logic | `verify.py:182-200` | Could be a simple dict accumulation |
| 3 | `_file_lock` context manager | `config.py:97-109` | No-op on non-POSIX; for single-user CLI tool |

---

## 3. ДУБЛИРОВАНИЕ: КОГДА ОДНО И ТО ЖЕ ПОВТОРЯЕТСЯ

### Top-10 highest-impact redundancies

| # | Что | Где | Suggested fix |
|---|-----|-----|---------------|
| **1** | `cfg.load_config()` + `save_config()` цикл | `cli.py`, `cascade.py`, `probe.py`, `refresh.py`, `health.py` — **10+ мест** | `with cfg.transaction() as cfg_obj:` context manager |
| **2** | `HEALTH_URL` / `DEFAULT_URLTEST_URL` | `singbox.py:17`, `free.py:9`, `models/config.py:7` — **3 раза** | Один источник в `models/config.py` |
| **3** | `_get_egress_ip` | `singbox.py:242-248`, `free.py:74-80` — **идентичны** | Один shared utility |
| **4** | `httpx.Client` creation | `singbox.py`, `nodes.py`, `cascade.py`, `bridge.py`, `probe.py`, `free.py`, `warp.py`, `self_update.py` — **8+ раз** | `core/http.py` factory |
| **5** | `IPIFY_URL` | `warp.py:15`, hardcoded в 3 файлах | Один constant |
| **6** | `_write_pid` / `save_pid` / `load_pid` / `save_daemon_pid` / `load_daemon_pid` | `proxy.py:74-110` — 5 функций | `core/pid.py` utility |
| **7** | `subprocess.Popen` sing-box run | `singbox.py:207-211`, `proxy.py:21-25` — одинаковый паттерн | Extract `_run_singbox()` |
| **8** | `subprocess.run` sing-box check | `verify.py:86-88` | Extract `_check_singbox_config()` |
| **9** | `(host, port)` tuple | `warp.py`, `free.py`, `singbox.py`, `proxy.py`, `verify.py` — 5+ мест | `Endpoint` type alias |
| **10** | `"No nodes configured"` | `cli.py:249,286,548`, `cascade.py:130`, `probe.py:155` — **6 мест** | `MESSAGES` constant |

### Duplicate CLI test files

**`tests/unit/cli/test_cli.py` + `tests/unit/cli/test_cli_commands.py`** — `test_cli.py` (54 lines) tests `logs` command, overlapping with `test_cli_commands.py`. Consolidate.

### Duplicate `status` output

**`status --json --verbose`** vs **`warp status --json`** — `cli.py:358-465` vs `cli.py:800-822`. The `warp status` output is a subset of `status --json --verbose`.

---

## 4. НЕПРОТЕСТИРОВАННЫЕ И НЕВИСПОЛЬЗУЕМЫЕ ФИЧИ

### CLI commands without test coverage

| Команда | Файл | Статус |
|---------|------|--------|
| `config` (edit) | `cli.py:728-737` | Нисколько не тестируется |
| `self-update` | `cli.py:828-853` | Модуль тестируется, но CLI command нет |
| `warp install` (реальный путь) | `cli.py:760-770` | Тестируется только "already installed" |

### Features that exist but may not be needed

**1. `openrot config edit`** — Opens `$EDITOR` to edit YAML. Could be replaced by `openrot profile set` commands for most operations.

**2. `openrot self-update`** — Downloads and installs new binary. Complex code path with 0 CLI test coverage. If the project doesn't ship releases frequently, this is dead weight.

**3. `warp install` command** — Downloads WARP package. Only tested for "already installed" case. The actual download+install path is untested.

**4. `node_from_records()`** — Dead function (covered in Section 1).

**5. 20+ fields on `Config`** — Many are internal constants (`bridge_retry_statuses`, `bridge_max_concurrent`, `deduplicate_by_ip`) that users rarely change. These could be code defaults instead of persisted config fields.

---

## 5. КОНКРЕТНЫЙ ПЛАН РЕФАКТОРИНГА (по приоритету)

### Фаза 1: Удалить мёртвый код (1 день)
```
[x] Удалить node_from_records()
[x] Исправить check_for_update() updated=False bug
[x] Удалить TypeVar в verify.py
[x] Удалить пустые __init__.py (или добавить __all__)
[x] Удалить закомментированные enum'ы
```

### Фаза 2: Убрать over-engineering (2-3 дня)
```
[x] Заменить LogOptions, RealityTLSOptions, UTLSOptions, 
    TransportOptions, MixedInbound на dict construction
[x] Удалить обёрточные функции (start_proxy, _log, _running_level, 
    refresh_profile, save_pid/load_pid duplicates)
[x] Снять Bridge(ThreadingHTTPServer) наследование
[x] Заменить Enums на string constants (WarpStatus, ActiveLevel)
[x] Удалить update_config[T] generic
```

### Фаза 3: Устранить дублирование (2-3 дня)
```
[x] Создать cfg.transaction() context manager
[x] Вынести HEALTH_URL, IPIFY_URL в один constant-файл
[x] Объединить _get_egress_ip в один utility
[x] Создать core/http.py client factory
[x] Создать core/pid.py для PID-операций
[x] Объединить test_cli.py в test_cli_commands.py
```

### Фаза 4: Добавить тесты для существующих фич (1-2 дня)
```
[x] Добавить тесты для config_edit
[x] Добавить тесты для self_update_cmd
[x] Добавить тесты для warp install path
```

### Фаза 5: Config simplification (1 день)
```
[x] Вынести internal constants (bridge_retry_statuses, etc.) 
    из Config в код defaults
[x] Разделить Config на user-facing и internal
```

---

## 6. ОЦЕНКА РАЦИОНАЛЬНОСТИ КАЖДОГО МОДУЛЯ

### Модуль | Оценка | Комментарий
|---------|--------|-----------|
| `cli.py` (856 LOC) | ⚠️ Слишком большой | 7 ответственностей. Нужен split на `cli/profile.py`, `cli/warp.py`, `cli/status.py` |
| `core/cascade.py` (347 LOC) | ⚠️ Слишком большой | 6 ответственностей. Нужен split |
| `core/bridge.py` | ✅ Нормальный | Чистый модуль, хорошая документация |
| `core/verify.py` | ✅ Нормальный | Но 50 workers — слишком много |
| `core/health.py` | ⚠️ | `select_node()` здесь — не по теме |
| `core/probe.py` | ❌ Нужен удалить | Чисто презентационный код → `cli/probe.py` |
| `core/singbox.py` | ⚠️ | 6 Pydantic-моделей для dict-структур |
| `core/proxy.py` | ⚠️ | PID-операции должны быть отдельно |
| `core/refresh.py` | ✅ Нормальный | Чистый модуль |
| `core/daemon.py` | ✅ Нормальный | Чистый модуль |
| `models/config.py` | ✅ Нормальный | Центральная модель |
| `models/node.py` | ⚠️ | `Node.raw` — primitive obsession |
| `models/profile.py` | ✅ Нормальный | |
| `models/enums.py` | ⚠️ | Enums → string constants |
| `providers/vless.py` | ✅ Нормальный | Хороший парсер |
| `providers/warp.py` | ⚠️ | `WarpStatus` enum → string; boolean returns |
| `providers/free.py` | ✅ Нормальный | |
| `self_update.py` | ⚠️ | Слишком сложный; `_check_services`, `_stop_services`, `_restart_services` — inline |
| `config.py` | ⚠️ | Re-exports models; file locking could be simpler |
| `opencode.py` | ✅ Нормальный | Простой модуль |
| `signals.py` | ✅ Нормальный | Простой модуль |
| `log.py` | ✅ Нормальный | Простой модуль |
| `tests/unit/cli/test_cli.py` | ❌ Удалить | Дублирует test_cli_commands.py |

---

**Итого**: ~85 находок рациональности. Основной паттерн — избыточная сложность в ядре (Pydantic-модели для dict-структур, обёрточные функции, enum-оверкус), дублирование конфигурации, и несколько мёртвых компонентов. Устранение фазами 1-3 уберёт ~60% проблем.