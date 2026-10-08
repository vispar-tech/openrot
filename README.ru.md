# openrot

![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-brightgreen)
![Coverage](https://img.shields.io/badge/Coverage-%3E85%25-brightgreen)
![Built for opencode](https://img.shields.io/badge/Built%20for-opencode-7c3aed)

[English](README.md) · **Русский**

Локальный WARP-мост. openrot поддерживает соединение с Cloudflare WARP и
поднимает loopback HTTP-мост, который форвардит трафик opencode через него. Один
файл конфигурации, одна движущаяся часть: WARP.

> **Большое спасибо opencode за то, что он существует.** Проект строится
> вокруг него: openrot поднимает loopback-мост, чтобы opencode (и любой
> OpenAI-совместимый клиент) продолжал работать на бесплатных
> `opencode/*`-провайдерах с автоматической саморотацией при 429.

```
opencode ──HTTP──▶ openrot bridge (127.0.0.1:7891)
                        │
                        ▼
               WARP SOCKS5 (127.0.0.1:40000)
                        │
                        ▼
               bridge_upstream (https://opencode.ai/zen)
```

## Зачем это нужно

Бесплатные провайдеры opencode лимитируют по сессии и IP. openrot держит вас на
соединении с WARP и ставит перед opencode локальный **bridge**, который
ротирует IP WARP в тот момент, когда апстрим отвечает `429` — долгие сессии
продолжаются без ручного вмешательства. Ничего не хостится на сервере; всё
работает на вашей машине.

## Быстрая установка

**Автономный бинарь** (macOS / Linux) — скачивает готовый релиз, ставит
`warp-cli` и поднимает WARP:

```bash
curl -fsSL https://raw.githubusercontent.com/vispar-tech/openrot/main/install.sh | bash
```

**Docker** (WARP по-прежнему работает на хосте) — ставит зависимости и запускает
образ из GHCR:

```bash
curl -fsSL https://raw.githubusercontent.com/vispar-tech/openrot/main/install-docker.sh | bash
```

> Первый способ ставит автономный бинарь в `~/.local/lib/openrot`
> (симлинк в `~/.local/bin/openrot`).
> Если warp-cli у вас уже есть, запуск можно пропустить:
> `OPENROT_SKIP_WARP=1 ... | bash`.

## Требования

- **warp-cli** (Cloudflare WARP) — обязателен в рантайме; работает на хосте, а
  не внутри контейнера:
  - macOS: `brew install --cask cloudflare-warp`
  - Linux: [пакетный репозиторий Cloudflare](https://pkg.cloudflareclient.com/)
  - `openrot warp install` печатает те же инструкции
- Python 3.14+ и [Poetry](https://python-poetry.org) — только для запуска из
  исходников

## Установка (из исходников)

```bash
poetry install
poetry run openrot --help
```

## Быстрый старт

```bash
# 1. подключите WARP (однократно; warp-cli должен быть установлен)
poetry run openrot warp on

# 2. запустите локальный bridge (127.0.0.1:7891); Ctrl-C останавливает
poetry run openrot start bridge
# ...или как фоновый daemon
poetry run openrot start bridge --daemon

# 3. статус / ротация / остановка
poetry run openrot status
poetry run openrot rotate
poetry run openrot stop
```

## Команды

| Команда | Описание |
| --- | --- |
| `openrot start bridge [--daemon]` | Запустить bridge opencode (в фореграунде или в фоне с `--daemon`) |
| `openrot stop [-y\|--yes]` | Отключить WARP и остановить bridge-daemon |
| `openrot status [--json] [-v\|--verbose]` | Статус WARP и bridge (--verbose = полная таблица диагностики) |
| `openrot rotate` | Сменить IP WARP (переподключается и проверяет, что адрес действительно изменился) |
| `openrot warp on\|off\|install` | Подключить / отключить WARP; `install` печатает инструкции по установке warp-cli |
| `openrot warp status [--json]` | Статус WARP и исходящий IP |
| `openrot logs [-n N] [--follow/--no-follow]` | Читать лог openrot (по умолчанию — следить) |
| `openrot run -- <cmd>` | Запустить команду с proxy-переменными, указывающими на WARP (поднимет WARP при необходимости) |
| `openrot config` | Открыть конфиг в $EDITOR (по умолчанию vim) |
| `openrot provider [--action on\|off\|auto] [--config PATH]` | Переключить провайдер opencode в opencode.jsonc |
| `openrot self-update [-y\|--yes]` | Обновить бинарь openrot до последнего релиза |
| `openrot --version` | Показать версию и выйти |

### `warp off` — временное состояние

`openrot warp off` немедленно отключает WARP, и `openrot status` показывает
«disconnected» — но пока bridge работает, его health loop переподключает WARP на
следующей проверке: в пределах `health_interval` (по умолчанию 30 секунд). Если
bridge не запущен, никто его не переподключит. `openrot stop` отключает WARP
*и* останавливает bridge-daemon, поэтому стек остаётся выключенным.

## opencode (bridge)

opencode общается с openrot через loopback-**bridge** — без лаунчера и без
магии с конфигами. Поднимите bridge и укажите на него opencode, слив один
ключ в конфиг:

```bash
openrot start bridge           # WARP + bridge, фореграунд; Ctrl-C для остановки
openrot start bridge --daemon  # или как фоновый daemon-процесс
```

Добавьте это в `~/.config/opencode/opencode.json`. Оверрайд ограничен
**встроенным провайдером `opencode`** — тем, что обслуживает дефолтные
бесплатные `opencode/*` модели, — поэтому эти модели идут через bridge
с саморотацией на 429, а всё остальное не меняется:

```json
{ "provider": { "opencode": { "options": { "baseURL": "http://127.0.0.1:7891/v1" } } } }
```

`bridge_port` по умолчанию `7891`. opencode видит только обычный loopback-HTTP —
без TLS, без proxy-переменных, ничего не нужно восстанавливать; TLS на участке
до апстрима открывает сам bridge через WARP. Фоновый bridge-daemon
останавливается через `openrot stop`; его логи пишутся в
`~/.config/openrot/openrot.log` (см. `openrot logs`).

```bash
openrot status           # bridge: running (http://127.0.0.1:7891/v1)
curl -s http://127.0.0.1:7891/v1/models
```

## Bridge (саморотация при 429)

Прокси-туннель перенаправляет трафик opencode «вслепую», поэтому он не видит,
что провайдер вернул HTTP `429` (запрос уже внутри TLS-CONNECT туннеля). Чтобы
автоматически менять перегруженный IP, укажите opencode на локальный **bridge**:
loopback-прокси, совместимое с OpenAI (`baseURL`), которое форвардит каждый
запрос *через SOCKS5-соединение WARP* к `bridge_upstream` и следит за статусом
апстрима. При `429` с телом `FreeUsageLimitError` он ротирует IP WARP
(`openrot rotate`) и повторяет запрос; остальные настроенные статусы ретрая
сначала ждут по `Retry-After`. Число повторов ограничено
`bridge_retry_attempts`.

Апстрим и порт настраиваются — см. поля конфига `bridge_port` (по умолчанию
`7891`) и `bridge_upstream` (по умолчанию `https://opencode.ai/zen`; путь
входящего запроса, например `/v1/chat/completions`, добавляется к этой базе),
либо env-переопределения `OPENROT_BRIDGE_PORT` и `OPENROT_UPSTREAM`.

### Сессионные заголовки бесплатного тира

Бесплатный тир opencode ориентируется на заголовок `X-Opencode-Session`:
голый запрос без него попадает в анонимную квоту и может быть отклонён с
`FreeUsageLimitError`. Когда `bridge_inject_session` включён (по умолчанию),
bridge добавляет в каждый запрос к апстриму (если их ещё нет):

- `X-Opencode-Session` — `ses_` + 26 цифр
- `X-Opencode-Request` — `msg_` + 24 alnum
- `User-Agent` opencode (не-opencode заменяется)
- заглушки в массиве `tools` тел чатов (гейтвею бесплатного тира нужно
  минимум два)

Значения, присланные клиентом, никогда не перезаписываются. Выключить можно
через `bridge_inject_session: false` в конфиге или
`OPENROT_BRIDGE_INJECT_SESSION=0`:

```bash
# через bridge: заголовки добавляются автоматически
curl -s http://127.0.0.1:7891/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{"model":"opencode/sonnet","messages":[{"role":"user","content":"hi"}]}'

# напрямую в апстрим: добавьте заголовок сами
curl -s https://opencode.ai/zen/v1/chat/completions \
  -H 'X-Opencode-Session: ses_<26 цифр>' \
  -H 'Content-Type: application/json' -d '{"model":"opencode/sonnet","messages":[{"role":"user","content":"hi"}]}'
```

## Конфигурация

Хранится в `~/.config/openrot/config.yaml`.
Переопределяется переменными `OPENROT_DIR` (базовый каталог) и `OPENROT_CONFIG`
(полный путь).

```yaml
health_interval: 30      # секунды между проверками здоровья WARP (health loop)
active_level: none       # none | warp (пишется при start/stop)
bridge_port: 7891        # loopback-порт для bridge opencode
bridge_upstream: "https://opencode.ai/zen"  # путь запроса добавляется к этой базе
bridge_retry_statuses: [429]   # статусы апстрима, вызывающие ретрай
bridge_retry_attempts: 1       # максимум повторов на запрос
bridge_max_concurrent: 3       # максимум параллельных запросов к апстриму
bridge_min_interval: 0.5       # минимум секунд между стартами запросов к апстриму
bridge_inject_session: true    # инъекция сессионных заголовков / UA / tools
```

### Переопределения через переменные окружения

| Переменная | Эффект |
| --- | --- |
| `OPENROT_DIR` | базовый каталог (по умолчанию `~/.config/openrot`) |
| `OPENROT_CONFIG` | полный путь файла конфигурации |
| `OPENROT_LISTEN` | адрес бинда bridge (по умолчанию `127.0.0.1`; в Docker — например `0.0.0.0`) |
| `OPENROT_BRIDGE_PORT` | → `bridge_port` |
| `OPENROT_UPSTREAM` | → `bridge_upstream` |
| `OPENROT_BRIDGE_RETRY_STATUSES` | → `bridge_retry_statuses` (список int через запятую) |
| `OPENROT_BRIDGE_RETRY_ATTEMPTS` | → `bridge_retry_attempts` |
| `OPENROT_BRIDGE_MAX_CONCURRENT` | → `bridge_max_concurrent` |
| `OPENROT_BRIDGE_MIN_INTERVAL` | → `bridge_min_interval` |
| `OPENROT_BRIDGE_INJECT_SESSION` | → `bridge_inject_session` (bool) |
| `OPENROT_WARP` | имя/путь бинарника warp-cli (по умолчанию `warp-cli`) |
| `OPENROT_WARP_HOST` / `OPENROT_WARP_PORT` | SOCKS5-адрес WARP (по умолчанию `127.0.0.1:40000`) |
| `OPENROT_WARP_ROTATE_DELAY` / `OPENROT_WARP_ROTATE_ATTEMPTS` | темп ротации: секунды между попытками / максимум попыток (по умолчанию `10` / `5`) |
| `OPENROT_DEBUG_REQ` | подробное логирование запросов bridge (секреты в auth-заголовках маскируются) |

Пути рантайма, все внутри базового каталога: `config.yaml`,
`openrot.log`, `openrot-bridge.daemon.pid`.

## Установка (самостоятельно / Docker)

Оба инсталлера авто-ставят `warp-cli` и поднимают WARP на хосте в proxy-режиме.

**Автономный бинарь** (PyInstaller, Python не нужен):

```bash
curl -fsSL https://raw.githubusercontent.com/vispar-tech/openrot/main/install.sh | bash
# или, из репозитория:
./install.sh
```

Переменные окружения: `OPENROT_BIN_URL`, `OPENROT_VERSION`, `OPENROT_PREFIX`,
`OPENROT_SKIP_WARP`. На macOS при первом запуске потребуется разрешить
системное расширение / VPN.

**Docker** (не ставится автоматически; скрипт напечатает инструкцию):

```bash
./install-docker.sh
make install-docker # то же самое
```

Переменные окружения: `OPENROT_IMAGE`, `OPENROT_BRIDGE_PORT`,
`OPENROT_CONFIG_DIR`, `OPENROT_WARP_HOST`, `NETWORK`.

## Docker

**WARP живёт на хосте, а не в контейнере.** `warp-cli` требует TUN-устройство
и сетевой стек ОС, поэтому не может поднять туннель внутри Docker. Контейнер
ходит к WARP хоста через его SOCKS5-прокси; образ запускает bridge (только
bridge — больше openrot ничего не запускает).

### Сначала поднимите WARP на хосте

Перед `make docker-run` один раз запустите WARP на хосте в proxy-режиме:

```bash
poetry run openrot warp on
```

### Makefile (для разработки)

```bash
make docker-run              # собирает образ и запускает openrot, подключённый к WARP хоста
make docker-sh               # интерактивная оболочка в образе (конфиг смонтирован так же)
```

По умолчанию контейнер использует bridge-сеть и указывает на WARP хоста через
`OPENROT_WARP_HOST=host.docker.internal` (порт хоста 40000):

- macOS (Docker Desktop) — работает из коробки.
- Linux — Makefile добавляет `--add-host host.docker.internal:host-gateway`.
- На Linux можно вместо этого запустить `NETWORK=host make docker-run`: общий
  loopback хоста, и WARP отвечает напрямую на `127.0.0.1:40000`.

### Запуск вручную

```bash
docker build -t openrot .
docker run --rm -it \
  -p 7891:7891 \
  --add-host host.docker.internal:host-gateway \
  -e OPENROT_WARP_HOST=host.docker.internal \
  -v $HOME/.config/openrot:/root/.config/openrot \
  openrot
```

### Что переопределяется / прокидывается

- **Конфиг и данные** шарятся через mount `-v $HOME/.config/openrot:...`
  (чтение/запись), поэтому конфиг один и тот же на хосте и в контейнере.
  `OPENROT_DIR` уже задан как `/root/.config/openrot` внутри образа
  (см. `Dockerfile`).
- **Порт**: bridge слушает на `7891`; другой порт хоста мапится через
  `-p $PORT_BRIDGE:7891`.
- **Адрес прослушивания**: bridge по умолчанию биндится на `127.0.0.1`;
  в образе задан `OPENROT_LISTEN=0.0.0.0`, чтобы опубликованный порт был
  доступен с хоста (переопределить — `-e OPENROT_LISTEN=127.0.0.1`).
- **Адрес WARP**: `OPENROT_WARP_HOST` (по умолчанию `127.0.0.1`) и
  `OPENROT_WARP_PORT` (по умолчанию `40000`) указывают openrot, где находится
  SOCKS5-прокси WARP хоста. В контейнере задайте `OPENROT_WARP_HOST` = хост.

Если WARP хоста недоступен, bridge всё равно стартует — health loop продолжает
пробовать подключение каждые `health_interval` — и `openrot status` показывает
`connectivity: FAIL`, пока WARP не поднимется.

## Разработка

- Тесты: `poetry run pytest` (или `make check` — ruff + mypy + coverage).
- Норма покрытия: локально держим отчёт **выше 85 %**; в CI порог снижен
  (75 %) как нижний предел.

## Решение проблем

**`status` показывает `connectivity: FAIL (WARP not forwarding traffic)`**
Проверьте warp-cli: `openrot warp status`. Если warp-cli отсутствует —
`openrot warp install`, затем `openrot warp on`.

**Я выполнил `openrot warp off`, но оно переподключается само**
Ожидаемо, пока работает bridge: health loop переподключает WARP в пределах
`health_interval` (по умолчанию 30 секунд). Чтобы выключить весь стек —
`openrot stop`, либо сначала остановите bridge.

**`bridge port 127.0.0.1:7891 is already in use; not starting`**
Порт занят другим openrot (или зависшим daemon'ом). Выполните
`openrot stop` или измените `bridge_port` / `OPENROT_BRIDGE_PORT`.

**Bridge запущен, но запросы к апстриму возвращают 502**
Не удалось получить ответ апстрима — обычно WARP выключен или недоступен.
`openrot status --verbose` печатает таблицу диагностики; `openrot warp on`
поднимет WARP обратно. Для логов по запросам задайте `OPENROT_DEBUG_REQ=1` и
следите за ними через `openrot logs`.

**В Docker контейнер не достаётся до WARP**
Контейнер должен указывать на хост: `OPENROT_WARP_HOST=host.docker.internal`
при bridge-сети (так делает Makefile), либо запускайте на Linux через
`NETWORK=host make docker-run`, чтобы увидеть WARP хоста на
`127.0.0.1:40000` напрямую.
