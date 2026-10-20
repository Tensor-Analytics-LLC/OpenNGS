FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NGS_STATE_DIR=/state NGS_DATA_DIR=/data NGS_USE_BUNDLED_SAMPLES=1 NGS_ALLOWED_HOSTS=127.0.0.1,localhost
WORKDIR /opt/openngs
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home openngs && mkdir /state /data && chown openngs:openngs /state /data
COPY backend ./backend
COPY frontend ./frontend
COPY demo-data ./demo-data
USER openngs
EXPOSE 8766
CMD ["python", "-m", "uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8766", "--no-access-log"]
