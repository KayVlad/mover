FROM python:3.12-slim
WORKDIR /app
COPY app ./app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MOVER_BIND=0.0.0.0 MOVER_STATE=/state
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"
CMD ["python", "-m", "app.server"]
