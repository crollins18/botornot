# The service (web page, schedule, workers) uses both images; see compose.yaml and ansible/deploy.yml.
# By hand, for a single scan from the command line:
# docker build -t botornot .                                    (about 200 MB)
# docker build -t botornot:meaning --build-arg MEANING=1 .      (adds CPU PyTorch for --meaning; much larger)
# docker run --rm -v botornot-data:/data botornot @SomeChannel
# docker run --rm -v botornot-data:/data -v botornot-models:/models botornot:meaning @SomeChannel --meaning
FROM python:3.14-slim

# lets `manage.yml -e do=clean` find old builds of this image, and only this image
LABEL app=botornot

ARG MEANING=0
# PYTHONUNBUFFERED: progress lines show up in `docker logs` as they happen. HF_HOME: where the --meaning model is kept.
ENV PYTHONUNBUFFERED=1 HF_HOME=/models

COPY requirements.txt requirements-service.txt requirements-meaning.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt -r /app/requirements-service.txt \
    && if [ "$MEANING" = 1 ]; then \
         pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
         && pip install --no-cache-dir -r /app/requirements-meaning.txt; \
       fi
COPY botornot.py orchestrator.py worker.py /app/
COPY templates /app/templates

# CSVs and the service's database are written to /data, the --meaning model to /models: mount a volume
# at each to keep them (compose.yaml does)
RUN useradd --uid 1000 --create-home scan && mkdir /data /models && chown scan /data /models
USER scan
WORKDIR /data
ENTRYPOINT ["python", "/app/botornot.py"]
