FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY bot ./bot
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home botuser \
    && mkdir -p /app/data && chown botuser:botuser /app/data
USER botuser
CMD ["python", "-m", "bot.main"]
