FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Bangkok
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY crystal_api.py notifiers.py watcher.py ./
VOLUME ["/app/data"]
ENV STATE_FILE=/app/data/state.json
CMD ["python", "watcher.py"]
