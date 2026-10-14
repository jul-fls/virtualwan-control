FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VWAN_DATA_DIR=/data
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
COPY virtualwan/ virtualwan/
COPY examples/ examples/
COPY tools/ tools/
COPY LICENSE NOTICE ./
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health',timeout=3)"
CMD ["python","-m","virtualwan.server"]
