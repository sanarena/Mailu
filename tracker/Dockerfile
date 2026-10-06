# Mail tracker image (arm64 for the hetzner k3s nodes; build on the Pi).
# Stage 1 converts the free DB-IP Lite CSV into the compact lookup file.
FROM python:3.13-slim AS geo
WORKDIR /build
COPY tools/build_geo.py tools/build_geo.py
ARG GEO_CSV_URL=https://download.db-ip.com/free/dbip-city-lite-2026-10.csv.gz
ADD ${GEO_CSV_URL} /build/dbip.csv.gz
RUN python3 tools/build_geo.py /build/dbip.csv.gz /build/location.dat \
 && rm /build/dbip.csv.gz

# Stage 2: stdlib-only runtime, non-root.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY mail_tracker/ ./mail_tracker/
COPY --from=geo /build/location.dat ./geo/location.dat
RUN useradd --system --uid 10000 --no-create-home tracker \
 && chown -R 10000:10000 /app
USER 10000:10000
EXPOSE 8899 8080
CMD ["python3", "-m", "mail_tracker.server"]
