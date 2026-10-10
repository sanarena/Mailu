# Mail tracker image (arm64 for the hetzner k3s nodes; build on the Pi).
# Build context is this directory plus a prebuilt location.dat:
#   python3 tools/build_geo.py <dbip-city-lite.csv.gz> location.dat
# (converting takes ~1 min and ~2GB RAM; keep it out of the image build).
# Stdlib-only runtime, non-root.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY mail_tracker/ ./mail_tracker/
COPY location.dat ./geo/location.dat
RUN useradd --system --uid 10000 --no-create-home tracker \
 && chown -R 10000:10000 /app
USER 10000:10000
EXPOSE 8899 8080
CMD ["python3", "-m", "mail_tracker.server"]
