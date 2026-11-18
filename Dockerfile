FROM python:3.12-slim

# ffmpeg for audio/frame extraction
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# faster-whisper downloads the model on first use; mount /models as a volume so it persists.
ENV HF_HOME=/models \
    WHISPER_MODEL_DIR=/models \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
