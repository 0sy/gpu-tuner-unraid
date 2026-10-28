FROM python:3.12-slim

RUN pip install --no-cache-dir \
    --root-user-action=ignore \
    nvidia-ml-py

RUN groupadd --gid 10001 gputuner \
 && useradd \
      --uid 10001 \
      --gid 10001 \
      --home-dir /home/gputuner \
      --create-home \
      --shell /usr/sbin/nologin \
      gputuner

WORKDIR /opt/gpu-tuner

COPY gpu_tuner/ ./gpu_tuner/
COPY web/ ./web/
COPY gpu-tuner gpu-tunerd ./

RUN chmod -R a+rX /opt/gpu-tuner \
 && chmod 755 /opt/gpu-tuner/gpu-tuner \
              /opt/gpu-tuner/gpu-tunerd

ENV PYTHONUNBUFFERED=1
# Persistent configuration, GPU state and WebUI credentials.
# The entrypoint initializes these directories on first launch.
ENV HOME=/data/ui
VOLUME ["/data"]

EXPOSE 8765

COPY container/entrypoint.py /usr/local/bin/gpu-tuner-entrypoint

RUN chmod 755 /usr/local/bin/gpu-tuner-entrypoint

ENTRYPOINT ["python", "/usr/local/bin/gpu-tuner-entrypoint"]
