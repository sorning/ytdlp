FROM python:3.10-slim

# 安装依赖
RUN apt update && apt install -y ffmpeg aria2 redis && \
    pip install --no-cache-dir yt-dlp flask flask-cors gunicorn celery redis

WORKDIR /app
COPY app.py /app/

# 启动 Gunicorn 来运行 Flask
CMD ["gunicorn", "-w", "4", "-t", "600", "-b", "0.0.0.0:5000", "app:app"]
