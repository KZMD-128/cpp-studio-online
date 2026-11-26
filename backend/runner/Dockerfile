FROM python:3.13-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ gdb ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin cppworker
WORKDIR /opt/cppstudio
COPY cppstudio_core.py /opt/cppstudio/cppstudio_core.py
COPY worker.py /opt/cppstudio/worker.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 10001:10001
ENTRYPOINT ["python3", "/opt/cppstudio/worker.py"]
