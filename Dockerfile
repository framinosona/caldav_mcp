FROM python:3.11-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir poetry==1.8.3 poetry-plugin-export
COPY pyproject.toml README.md ./
COPY src ./src
RUN poetry lock && \
    poetry export --without-hashes --only main -f requirements.txt -o requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.11-slim
RUN useradd --create-home --uid 1000 caldav
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY --from=builder /app/requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt && \
    rm -rf /wheels
COPY src ./src
ENV PYTHONPATH=/app/src
USER caldav
EXPOSE 8000
ENTRYPOINT ["python", "-m", "caldav_mcp.main"]
CMD ["--transport", "sse", "--host", "0.0.0.0", "--port", "8000"]
