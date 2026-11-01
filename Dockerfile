FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=production
ENV PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.lock.txt ./
RUN python -m pip install --no-cache-dir -r requirements.lock.txt \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid agent --no-create-home agent \
    && mkdir -p /data/artifacts /data/profile \
    && chown -R agent:agent /data
COPY --chown=agent:agent src ./src
COPY --chown=agent:agent scripts ./scripts
COPY --chown=agent:agent contracts ./contracts
USER agent
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
CMD ["python", "-m", "scripts.serve_api", "--host", "0.0.0.0", "--port", "8000"]
