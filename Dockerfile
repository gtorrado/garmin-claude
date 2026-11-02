FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
