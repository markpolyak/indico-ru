# Indico с русским языком и плагином autoaccept.
# Сборка локально: ./build.sh <версия>; в CI: .github/workflows/build.yml
# Основано на Dockerfile из https://github.com/indico/indico-containers (MIT, CERN).

ARG INDICO_VERSION=3.3.14
ARG INDICO_PLUGINS_VERSION=3.3.8

FROM --platform=$BUILDPLATFORM node:22-bookworm AS nodejs

########## 1. Сборка wheel Indico с ru_RU (нативно на машине сборки) ##########
FROM --platform=$BUILDPLATFORM python:3.12-bookworm AS builder
ARG INDICO_VERSION
ENV VENV=/opt/indico/.venv
SHELL ["/bin/bash", "-exo", "pipefail", "-c"]

RUN apt-get update && \
    apt-get -y install --no-install-recommends libxslt1-dev libxml2-dev libffi-dev libpcre3-dev libyaml-dev \
        build-essential libpq-dev libpango1.0-dev libjpeg62-turbo-dev git && \
    rm -rf /var/lib/apt/lists/*
# Indico 3.3.x собирается на Node.js 22 (в Debian bookworm — 18, не подходит)
COPY --from=nodejs /usr/local/bin/node /usr/local/bin/node
COPY --from=nodejs /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && node --version && npm --version
RUN curl -fsSL https://raw.githubusercontent.com/transifex/cli/master/install.sh | bash && mv tx /usr/local/bin/tx

RUN git clone --depth 1 --branch v${INDICO_VERSION} https://github.com/indico/indico.git /opt/indico/src
WORKDIR /opt/indico/src
RUN python -m venv $VENV && $VENV/bin/pip install -U --no-cache-dir pip setuptools wheel && \
    $VENV/bin/pip install --no-cache-dir -e '.[dev]'
ENV PATH="$VENV/bin:$PATH"
RUN npm ci

# Наши исправления исходников Indico (patches/*.patch)
COPY patches /opt/patches
RUN for p in /opt/patches/*.patch; do echo "apply $p"; git apply --verbose "$p"; done

# Запасной перевод (снимок перевода); если есть токен Transifex — заменяется свежим.
COPY fallback-translations/ru_RU/messages-all.po indico/translations/ru_RU/LC_MESSAGES/messages-all.po
# TX_PULL_ID меняется при каждой сборке с токеном -> перевод всегда скачивается заново
ARG TX_PULL_ID=none
RUN --mount=type=secret,id=tx_token,required=false \
    set +x; echo "tx pull id: ${TX_PULL_ID}"; \
    if [ -s /run/secrets/tx_token ]; then \
        export TX_TOKEN="$(cat /run/secrets/tx_token)"; indico i18n pull indico ru_RU && echo 'ru_RU: Transifex'; \
    else echo 'ru_RU: fallback (снимок перевода)'; fi
RUN indico i18n compile indico && \
    python ./bin/maintenance/update_moment_locales.py && \
    grep -q "moment/locale/ru'" indico/web/client/js/jquery/index.js && \
    python ./bin/maintenance/build-wheel.py indico --ignore-unclean && \
    ls -la dist/

# Плагин autoaccept: отдельный репозиторий, передаётся как build context `autoaccept`
# (локальный каталог или git-URL с тегом, см. build.sh и workflow)
COPY --from=autoaccept . /opt/plugins/autoaccept
RUN cd /opt/plugins/autoaccept && $VENV/bin/pip wheel --no-deps -w /opt/indico/src/dist .

########## 2. Рабочий образ (как официальный getindico/indico) ##########
FROM python:3.12-bookworm
ARG INDICO_PLUGINS_VERSION
ENV INDICO_VIRTUALENV="/opt/indico/.venv" INDICO_CONFIG="/opt/indico/etc/indico.conf"
ARG pip="/opt/indico/.venv/bin/pip"
USER root
SHELL ["/bin/bash", "-exo", "pipefail", "-c"]

RUN groupadd -r indico && useradd -r -g indico -m -d /opt/indico indico
RUN apt-get update && \
    apt-get -y install --no-install-recommends texlive-xetex libpq-dev postgresql-client vim less gcc gettext libldap2-dev && \
    rm -rf /var/lib/apt/lists/*

COPY scripts/uwsgi.ini /etc/uwsgi.ini
WORKDIR /opt/indico
RUN mkdir -p --mode=750 /opt/indico/etc /opt/indico/log /opt/indico/cache /opt/indico/archive && \
    mkdir -p --mode=777 /opt/indico/tmp && chown -R indico:indico /opt/indico && chmod g=u /etc/passwd

USER indico
RUN python -m venv ${INDICO_VIRTUALENV} && \
    ${pip} install --no-cache-dir --upgrade pip setuptools wheel && ${pip} install --no-cache-dir uwsgi
RUN --mount=type=bind,from=builder,source=/opt/indico/src/dist,destination=/tmp/dist \
    ${pip} install --no-cache-dir /tmp/dist/indico-*.whl "indico-plugins==${INDICO_PLUGINS_VERSION}" && \
    ${pip} install --no-cache-dir --no-deps /tmp/dist/indico_plugin_autoaccept-*.whl
RUN /opt/indico/.venv/bin/indico setup create-symlinks /opt/indico && \
    test -f /opt/indico/.venv/lib/python3.12/site-packages/indico/translations/ru_RU/LC_MESSAGES/messages.mo

COPY --chown=indico:indico scripts/run_indico.sh scripts/run_celery.sh scripts/docker_entrypoint.sh /opt/indico/
RUN chmod 755 /opt/indico/*.sh

LABEL org.opencontainers.image.source="https://github.com/markpolyak/indico-ru" \
      org.opencontainers.image.description="Indico with Russian (ru_RU) translation" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/opt/indico/docker_entrypoint.sh"]
EXPOSE 59999
