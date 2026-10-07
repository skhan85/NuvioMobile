FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -q ffsubsync
COPY server.py /server.py
ENV CACHE_DIR=/cache AUDIO_MINUTES=8 MAX_JOBS=2
EXPOSE 8080
CMD ["python3", "-u", "/server.py"]
