# indico-ru — Indico с русским языком

Docker-образ [Indico](https://getindico.io) с русским переводом интерфейса.
В официальных сборках Indico русского языка нет: перевод ведётся на
[Transifex](https://app.transifex.com/indico/indico/) (язык `ru_RU`), но встроить его можно только
при сборке Indico из исходников. Этот репозиторий делает именно это.

*English summary below.*

## Что в образе

- Indico указанной версии, собранный из исходников с переводом `ru_RU` (Python, JS и React-части
  интерфейса, а также локаль moment.js для дат в календарях).
- Набор официальных плагинов [`indico-plugins`](https://github.com/indico/indico-plugins).
- Плагин [`indico-plugin-autoaccept`](https://github.com/markpolyak/indico-plugin-autoaccept) —
  автоматическое принятие тезисов (включается в каждом мероприятии отдельно, по умолчанию выключен).
- Исправления Indico из [`patches/`](patches/), которых ещё нет в официальном релизе.

Образ совместим с `getindico/indico` и с docker-compose из
[indico-containers](https://github.com/indico/indico-containers): те же пути, скрипты запуска
(`/opt/indico/run_indico.sh`, `/opt/indico/run_celery.sh`) и порт 59999.

## Использование

```yaml
services:
  indico-web:
    image: ghcr.io/markpolyak/indico-ru:3.3.14
```

В `indico.conf`:

```python
DEFAULT_LOCALE = 'ru_RU'
PLUGINS = {'autoaccept', ...}   # если нужен плагин автопринятия
```

Теги: `<версия Indico>` (последняя сборка) и `<версия Indico>-<номер сборки>` (неизменяемый,
удобно для отката).

## Сборка

**В GitHub Actions:** Actions → *Build and publish image* → *Run workflow*, указать версию Indico.
Свежий перевод скачивается с Transifex, если в секретах репозитория задан `TX_TOKEN`
(API-токен Transifex); без него используется снимок перевода из `fallback-translations/`.

**Локально** (нужен Docker с buildx):

```sh
./build.sh 3.3.14
```

Токен Transifex, если есть, — в файле `~/.indico-tx-token`. Переменные:
`AUTOACCEPT_SRC` — источник плагина (git-URL с тегом или локальный каталог),
`TX_REFRESH=0` — не скачивать перевод заново (взять из кеша сборки).

## Патчи

- `0001-fix-double-encoded-sender-name.patch` — при подмене отправителя (`SMTP_ALLOWED_SENDERS`)
  имена с кириллицей кодировались дважды, и в поле «От кого» появлялось `=?utf-8?b?...?=`.
  Предложено в Indico: [indico/indico#7910](https://github.com/indico/indico/pull/7910).

Если после обновления версии Indico патч перестал применяться (сборка падает на `git apply`),
скорее всего, исправление уже вошло в Indico — патч можно удалить.

## Обновление на сервере

После обновления версии Indico (не просто пересборки) нужны миграции БД и обновление тома со
статикой, если он вынесен в отдельный volume:

```sh
docker compose pull
docker compose rm -sf indico-web indico-celery indico-celery-beat indico-nginx
docker volume rm <project>_static-files          # статика старой версии
docker compose run --rm --no-deps indico-web bash -c 'indico db upgrade && indico db --all-plugins upgrade'
docker compose up -d
```

Перед этим — `pg_dump` базы.

---

## English summary

Docker image of [Indico](https://getindico.io) with the Russian (`ru_RU`) translation built in,
which is not part of the official Indico releases. Built from source with the latest translation
from Transifex, plus the official `indico-plugins`, the
[abstract auto-acceptance plugin](https://github.com/markpolyak/indico-plugin-autoaccept) and a few
fixes from [`patches/`](patches/). Drop-in compatible with `getindico/indico` and the
[indico-containers](https://github.com/indico/indico-containers) docker-compose setup.

Image: `ghcr.io/markpolyak/indico-ru:<indico version>`. Build it via the GitHub Actions workflow
(optionally with a `TX_TOKEN` secret for a fresh translation) or locally with `./build.sh <version>`.

The Dockerfile is based on [indico-containers](https://github.com/indico/indico-containers)
(MIT, © CERN).

## License

MIT
