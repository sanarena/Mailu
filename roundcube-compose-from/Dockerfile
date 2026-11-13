# Roundcube compose_from plugin package (arm64; build on the Pi).
# Same layout as the scheduler/tracking/backup packages: the webmail
# initContainer copies /pkg/{plugins,overrides} into the shared emptyDir.
FROM busybox:stable
COPY plugins/ /pkg/plugins/
COPY overrides/ /pkg/overrides/
CMD ["sh"]
