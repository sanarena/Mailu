# Roundcube mail_backup plugin package (arm64; build on the Pi).
# Same layout as the scheduler/tracking packages: the webmail
# initContainer copies /pkg/{plugins,overrides} into the shared
# emptyDir. BusyBox base provides the sh/cp/ls used by the
# initContainer command.
FROM busybox:stable
COPY plugins/ /pkg/plugins/
COPY overrides/ /pkg/overrides/
CMD ["sh"]
