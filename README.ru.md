# openrot

![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-brightgreen)
![Coverage](https://img.shields.io/badge/Coverage-%3E85%25-brightgreen)
![Built for opencode](https://img.shields.io/badge/Built%20for-opencode-7c3aed)

[English](README.md) · **Русский**

Локальный ротатор прокси. Один файл конфигурации определяет **профили**
(источники бесплатных нод) и их **ноды**. Трафик всегда идёт сначала через
**Cloudflare WARP** (наивысший приоритет, не является нодой), затем вниз по
цепочке: профили по своему `priority`, затем ноды по своему `priority`,
до первой живой. Когда нода падает, openrot ротирует на следующую.

> **Большое спасибо opencode за то, что он существует.** Проект строится
> вокруг него: openrot держит каскад бесплатных нод живым и поднимает
> loopback-bridge, чтобы opencode (и любой OpenAI-совместимый клиент)
> продолжал работать на бесплатных провайдерах с автоматической ротацией.

```
WARP (включён по умолчанию)
   │  warp_enabled: true, только на хосте (в Docker пропускается)
   ▼
Профиль A (priority 0) ── нода 0, нода 1, нода 2   ◀─ выбрать первую живую
Профиль B (priority 1) ── нода 0, нода 1
...
```

## Зачем это нужно

Публичные V2Ray-конфиги и списки прокси постоянно умирают. Вы добавляете
несколько источников как профили, а openrot поддерживает их живыми: скачивает
по расписанию, проверяет здоровье каждой ноды, выбирает лучшую и ротирует при
падении WARP или ноды.

## Требования

- Python 3.14+, [Poetry](https://python-poetry.org)
- **sing-box** — внешний бинарник, НЕ Python-зависимость:
  - macOS: `brew install sing-box`
  - [GitHub Releases](https://github.com/SagerNet/sing-box/releases)
- **warp-cli** (Cloudflare WARP) — опционально, только на хосте, в Docker нет

## Установка

```bash
poetry install
poetry run openrot --help
```

## Быстрый старт

```bash
# 1. добавьте профиль-источник (репозиторий ссылок vless:// или прокси)
poetry run openrot profile add gitlab https://gitlab.com/igareck/vpn-configs-for-russia/-/raw/main/WHITE-CIDR-RU-all.txt --kind relay --priority 0

# 2. скачайте его ноды
poetry run openrot update --relay

# 3. проверьте здоровье всех нод
poetry run openrot test

# 4. запустите локальный прокси (слушает 127.0.0.1:7890)
poetry run openrot start cascade
# в другом терминале:
curl -x http://127.0.0.1:7890 https://ifconfig.me

# 5. статус / ротация / остановка
poetry run openrot status
poetry run openrot rotate
poetry run openrot stop
```

## Команды

| Команда | Описание |
| --- | --- |
| `openrot profile add NAME URL --kind relay\|proxy [--priority N] [--interval S] [--disabled]` | Добавить профиль-источник |
| `openrot profile list [--json]` | Список профилей |
| `openrot profile set NAME [--priority N] [--interval S] [--enabled\|--disabled]` | Обновить приоритет, интервал обновления или состояние профиля |
| `openrot profile remove NAME` | Удалить профиль и его ноды |
| `openrot list [--alive] [--json]` | Список нод по профилям |
| `openrot test [--json]` | Проверка здоровья всех нод |
| `openrot update [--relay] [--proxy] [--name X]` | Скачать ноды для всех включённых профилей (по умолчанию; фильтры через флаги) |
| `openrot start cascade\|bridge [--daemon]` | Запустить каскад (в фореграунде логи идут в терминал; `--daemon` = в фоне) или `bridge` — сервер 429-ротации (в фореграунде или `--daemon` в фоне) |
| `openrot stop [-y]` | Остановить активный стек: WARP или локальный прокси, плюс bridge-даемон |
| `openrot status [--json] [-v\|--verbose]` | Текущий уровень и связность (--verbose = полная диагностика стека) |
| `openrot rotate` | Сменить IP WARP или выбрать следующую ноду |
| `openrot logs [-n N] [--follow/--no-follow]` | Следить за логами daemon, events и bridge одним потоком |
| `openrot config` | Открыть конфиг в $EDITOR (по умолчанию vim) |
| `openrot warp on\|off\|install` | Включить/выключить (подключить/отключить) WARP |
| `openrot warp status [--json]` | Статус WARP |
| `openrot run -- <cmd>` | Выполнить команду с переменными окружения прокси |
| `openrot probe <url>` | Прогнать `url` через активный стек, показывая каждый этап |

## opencode (bridge)

opencode общается с openrot через loopback-**bridge** — без лаунчера и без
магии с конфигами. Поднимите bridge и укажите на него opencode, слив один
ключ в конфиг:

```bash
openrot start bridge           # каскад + bridge, в фореграунде; Ctrl-C для остановки
openrot start bridge --daemon  # или как фоновый daemon-процесс
```

Добавьте это в `~/.config/opencode/opencode.json`. Оверрайд ограничен
**встроенным провайдером `opencode`** — тем, что обслуживает дефолтные
бесплатные `opencode/*` модели, — поэтому эти модели идут через bridge
с саморотацией на 429, а всё остальное не меняется:

```json
{ "provider": { "opencode": { "options": { "baseURL": "http://127.0.0.1:7891/v1" } } } }
```

`<bridge_port>` по умолчанию `7891` (поле конфига `bridge_port`). opencode видит
только обычный loopback-HTTP — без TLS, без proxy-переменных, ничего не нужно
восстанавливать; TLS на участке до апстрима терминирует сам bridge.
Фоновый bridge-daemon останавливается через `openrot stop` (лог:
`~/.config/openrot/openrot-bridge.log`).

```bash
openrot status           # bridge: running (http://127.0.0.1:7891/v1)
curl -s http://127.0.0.1:7891/v1/models
```

## Bridge (саморотация при 429)

Прокси-туннель перенаправляет трафик opencode «вслепую», поэтому он не видит,
что провайдер вернул HTTP `429` (запрос уже внутри TLS-CONNECT туннеля). Чтобы
автоматически менять перегруженную ноду, укажите opencode на локальный **bridge**:
loopback-прокси, совместимое с OpenAI (`baseURL`), которое форвардит каждый
запрос *через активный каскад* и следит за статусом апстрима. При `429`
он ротирует каскад (`openrot rotate` — следующая нода / WARP) и один раз
повторяет запрос прозрачно.

Апстрим и порт настраиваются — см. поля конфига `bridge_port` (по умолчанию
`7891`) и `bridge_upstream` (по умолчанию `https://opencode.ai/zen/v1` — тот же
endpoint, что opencode использует из коробки), либо
env-переопределения `OPENROT_BRIDGE_PORT` и `OPENROT_UPSTREAM`.

## Бесплатные источники (профили)

Профиль — это URL, отдающий список нод. `--kind` указывает, как openrot будет
его разбирать.

### Релейные профили (`--kind relay`)

Источники ссылок `vless://` — публичные репозитории и зеркала, публикующие
ежедневные дампы рабочих конфигов:

- [igareck/vpn-configs-for-russia](https://gitlab.com/igareck/vpn-configs-for-russia) — прямой raw-файл:
  ```
  https://gitlab.com/igareck/vpn-configs-for-russia/-/raw/main/WHITE-CIDR-RU-all.txt
  ```
- Ищите в GitHub/GitLab `vless config list` / `v2ray subscription` — большинство
  таких репозиториев подходит как relay-профиль.

```bash
poetry run openrot profile add gitlab <URL> --kind relay --priority 0
poetry run openrot update --relay
```

> Сейчас разбирается только `vless://`. Файлы со смесью других протоколов
> (hysteria2, vmess, ss) пока не поддерживаются. См. `NodeProtocol` в
> `models/enums.py`.

### Прокси-профили (`--kind proxy`)

Источники бесплатных http / socks5 прокси, строки вида `proto://host:port`:

- [proxifly/free-proxy-list](https://github.com/proxifly/free-proxy-list) — проверяется каждые 5 минут, отдаётся через CDN:
  ```
  https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt
  ```
  Только по протоколам: `.../proxies/protocols/http/data.txt`, `.../proxies/protocols/socks5/data.txt`.
- Подойдут и зеркала free-proxy-list, и Telegram-каналы с ежедневными дампами.

```bash
poetry run openrot profile add proxifly <URL> --kind proxy --priority 10
poetry run openrot update --proxy
```

Свободные прокси — самый нестабильный слой, поэтому им обычно задают **низкий
приоритет** (число `--priority` больше), чем relay-профилям: они подключаются,
только когда релейная цепочка исчерпана. `update --proxy` держит топ-20 живых
по задержке.

### Приоритет и обновление

- `--priority N`: меньше число, выше приоритет. Профили перебираются сверху
  вниз, затем ноды внутри профиля по своей `priority`.
- `--interval S`: секунды обновления профиля. По умолчанию — глобальный
  `update_interval`. `openrot start cascade` обновляет профили по
  истечении интервала. Ручной `openrot update` учитывает `--name` и фильтр
  по протоколу.
- `--disabled`: добавить профиль выключенным; включить можно правкой
  `config.yaml`.

## Конфигурация

Хранится в `~/.config/openrot/config.yaml`.
Переопределяется переменными `OPENROT_DIR` (базовый каталог), `OPENROT_CONFIG`
(полный путь), `OPENROT_PORT` (целое число), `OPENROT_SINGBOX_BIN` (имя/путь
до бинарника sing-box), `OPENROT_BRIDGE_PORT` (целое число) и `OPENROT_UPSTREAM`
(base URL апстрима bridge). Все стандартизированные поля — значения `StrEnum`.

```yaml
port: 7890
strategy: fallback        # fallback | urltest
urltest_url: "https://www.gstatic.com/generate_204"   # целевой URL для замера задержки
health_interval: 30
health_timeout: 5
fail_threshold: 3
update_interval: 3600     # секунды; интервал обновления по умолчанию
warp_enabled: true        # WARP = высший приоритет; выключить: 'openrot warp off'
profiles:
  - name: gitlab
    kind: relay           # relay | proxy
    url: https://gitlab.com/igareck/vpn-configs-for-russia/-/raw/main/WHITE-CIDR-RU-all.txt
    priority: 0           # меньше = выше
    enabled: true
    interval: null        # null → используется update_interval
    nodes:
      - id: "node-..."
        raw: "vless://..."
        protocol: vless   # vless | http | socks5
        priority: 0
        status: unknown   # unknown | alive | dead
        latency_ms: null
        fails: 0
        last_check: null
active_level: none        # none | warp | node
current_node_id: null
singbox_bin: "sing-box"
bridge_port: 7891         # loopback-порт для bridge opencode
bridge_upstream: "https://opencode.ai/zen/v1"
```

Латентность ноды — время HTTP-запроса к `urltest_url`
(по умолчанию `https://www.gstatic.com/generate_204`); релейные ноды
проверяются через sing-box, прокси — напрямую.

## Как проверяются ноды

`openrot update` и планировщик прогоняют каждую полученную ноду через пайплайн:

```
parse (дедуп vless:// / proto://host:port)
  -> доступность по TCP (параллельно, 50 воркеров)
  -> TLS-хендшейк (только для tls/reality/wss нод)
  -> sing-box check конфига (только relay)
  -> HTTP-проба urltest_url через ноду
```

Проба — запрос к `urltest_url` (по умолчанию `generate_204`); нода выживает
при ответе 2xx. Пробиваются все ноды, прошедшие предыдущие стадии (все
параллельно), выжившие ранжируются по задержке (время запроса), публикуется
**топ-20** (`TOP_LIMIT`) на профиль.

Прогресс печатается вживую: `update` показывает постадийный счётчик/бар
(`verify parse: 3/3`, `verify probe: 12/100`), `openrot probe <url>` печатает
те же стадии строками. `openrot test` заново проверяет опубликованные ноды
тем же пайплайном.

## Стратегии

- `fallback` — первая живая нода сверху вниз по приоритету профиля/ноды (по умолчанию)
- `urltest` — минимальная задержка среди живых нод

В foreground-режиме текущая нода перепроверяется каждые `health_interval`
секунд и ротируется при падении (`fail_threshold` сбоев подряд = нода dead).

## Установка (самостоятельно / Docker)

Оба инсталлера авто-ставят `warp-cli` и поднимают WARP на хосте в proxy-режиме.

**Автономный бинарь** (PyInstaller, Python не нужен):

```bash
./install.sh        # из репозитория
make installer      # то же самое
```

Переменные окружения: `OPENROT_BIN_URL`, `OPENROT_VERSION`, `OPENROT_PREFIX`,
`OPENROT_SKIP_WARP`. На macOS при первом запуске потребуется разрешить
системное расширение / VPN.

**Docker** (не ставится автоматически; скрипт напечатает инструкцию):

```bash
./install-docker.sh
make install-docker # то же самое
```

Переменные окружения: `OPENROT_IMAGE`, `OPENROT_PORT`, `OPENROT_CONFIG_DIR`,
`OPENROT_WARP_HOST`, `NETWORK`.

Доустановите и `sing-box`; openrot нуждается в нём для релейных
нод (`brew install sing-box` на macOS).

## Docker

**WARP живёт на хосте, а не в контейнере.** `warp-cli` требует
TUN-устройство и сетевой стек ОС, поэтому не может поднять туннель внутри
Docker. Контейнер ходит к WARP хоста через его SOCKS5-upstream, так что WARP и
цепочка нод работают вместе.

### Makefile (для разработки)

```bash
make docker-run              # собирает образ и запускает openrot, подключённый к WARP хоста
make docker-run MODE=both    # то же самое, но оба демона в фоне
make docker-sh               # интерактивная оболочка в образе (конфиг смонтирован так же)
```

### Режимы запуска

По умолчанию контейнер использует bridge-сеть и указывает на WARP хоста через
`OPENROT_WARP_HOST=host.docker.internal` (порт хоста 40000):

- macOS (Docker Desktop) — работает из коробки.
- Linux — Makefile добавляет `--add-host host.docker.internal:host-gateway`.
- На Linux можно вместо этого запустить `NETWORK=host make docker-run`: общий
  loopback хоста, и WARP отвечает напрямую на `127.0.0.1:40000`.

Контейнер принимает один аргумент — режим работы (CMD образа по умолчанию — `both`):

| Режим      | Что запускается                                                              |
|------------|------------------------------------------------------------------------------|
| `cascade`  | Прокси/каскад в фореграунде; лог событий стримится в `docker logs`.          |
| `bridge`   | Bridge 429-ротации в фореграунде (вместе с ним поднимается и каскад).        |
| `both`     | Каскад + bridge как фоновые демоны, запускаются одновременно; `openrot logs` стримит их в stdout и держит контейнер живым. |

```bash
docker run --rm -it ... openrot cascade   # только прокси, логи в фореграунде
docker run --rm -it ... openrot bridge    # только bridge, фореграунд
docker run --rm -it ... openrot both      # оба демона в фоне, логи через `openrot logs`
docker stop <container>                   # гасит супервизор; демоны умирают вместе с ним
```

### Запуск вручную

```bash
docker build -t openrot .
docker run --rm -it \
  -p 7890:7890 \
  -p 7891:7891 \
  --add-host host.docker.internal:host-gateway \
  -e OPENROT_WARP_HOST=host.docker.internal \
  -v $HOME/.config/openrot:/root/.config/openrot \
  openrot both
```

### Что переопределяется / прокидывается

- **Конфиг и данные** шарятся через mount `-v $HOME/.config/openrot:...`
  (чтение/запись), поэтому профиль, добавленный на хосте (или в контейнере),
  виден с обеих сторон. `OPENROT_DIR` уже задан как `/root/.config/openrot`
  внутри образа (см. `Dockerfile`).
- **Порты**: прокси слушает на `7890`, bridge — на `7891`; другой порт хоста
  мапится через `-p $PORT:7890` / `-p $PORT_BRIDGE:7891`.
- **Адрес прослушивания**: прокси и bridge по умолчанию биндятся на `127.0.0.1`;
  в образе задан `OPENROT_LISTEN=0.0.0.0`, чтобы опубликованные порты были
  доступны с хоста (переопределить — `-e OPENROT_LISTEN=127.0.0.1`).
- **Адрес WARP**: `OPENROT_WARP_HOST` (по умолчанию `127.0.0.1`) и
  `OPENROT_WARP_PORT` (по умолчанию `40000`) указывают openrot, где находится
  SOCKS5-прокси WARP хоста. В контейнере задайте `OPENROT_WARP_HOST` = хост.
- **`OPENROT_PORT` / `OPENROT_SINGBOX_BIN`**: задаются через `-e`, если нужно
  переопределить сам `openrot`; `sing-box` уже установлен в образе.
- **Режим при старте контейнера**: `docker run ... openrot cascade|bridge|both`
  или перезапуск существующего — `docker start -ai <name> <mode>` (режим станет
  командой контейнера).

### Сначала поднимите WARP на хосте

Перед `make docker-run` один раз запустите WARP на хосте в proxy-режиме:

```bash
poetry run openrot warp on
```

Дальше его сможет использовать и контейнер, и openrot на хосте. Если WARP хоста
недоступен, openrot падает на цепочку профилей + нод, а `status` показывается
`WARP: available on host only`.

## Разработка

- Тесты: `poetry run pytest` (или `make check` — ruff + mypy + coverage).
- Норма покрытия: локально держим отчёт **выше 85 %**; в CI порог снижен
  (75 %) как нижний предел.

## FAQ

**Что нужно хостить?**
Ничего. openrot только скачивает чужие списки, проверяет их и поднимает
локальный sing-box. На сервере ничего не хранится.

**Почему не одна нода?**
Бесплатные источники умирают. openrot относится к ним как к пулу с
приоритетами: когда верхняя нода падает, идёт ротация вниз по цепочке, а
профили скачиваются заново по интервалу.