# Mail backup image (arm64 for the hetzner k3s nodes; build on the Pi).
# Python stdlib + cryptography (Fernet for target passwords), non-root.
#   docker build --target test backup   # runs the unit tests
#   docker build -t sanarena/mail-backup:<ver> backup
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY mbackup/ ./mbackup/

FROM base AS test
COPY tests/ ./tests/
RUN python3 -m unittest discover -s tests -t . -v

FROM base
RUN useradd --system --uid 10000 --no-create-home mailbackup \
 && chown -R 10000:10000 /app
USER 10000:10000
EXPOSE 8080
CMD ["python3", "-m", "mbackup.server"]
