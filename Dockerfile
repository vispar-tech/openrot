FROM python:3.14.3-slim

ARG OPENROT_VERSION=0.0.0

ENV POETRY_VERSION=2.3.2
ENV POETRY_DYNAMIC_VERSIONING_BYPASS=${OPENROT_VERSION}
ENV OPENROT_DIR=/root/.config/openrot
ENV OPENROT_LISTEN=0.0.0.0

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}" && \
    poetry config virtualenvs.create false && \
    poetry install --only main --no-root --no-interaction

COPY . .
RUN poetry install --only main --no-interaction && rm -rf ~/.cache
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 7891
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["bridge"]