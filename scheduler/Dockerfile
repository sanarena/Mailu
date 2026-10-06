# Mail scheduler image (arm64 for the hetzner k3s nodes; build on the Pi).
# Stdlib-only runtime, non-root.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY sched/ ./sched/
RUN useradd --system --uid 10000 --no-create-home scheduler \
 && chown -R 10000:10000 /app
USER 10000:10000
EXPOSE 8080
CMD ["python3", "-m", "sched.server"]
