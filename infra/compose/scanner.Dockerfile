FROM clamav/clamav:stable@sha256:e19130cdebaeacc71fa46eab3ed6f69941f2f22aa5e08d55e9197f8edf43ce5e
COPY infra/compose/start-scanner.sh /usr/local/bin/lab-start-scanner
ENTRYPOINT ["/bin/sh", "/usr/local/bin/lab-start-scanner"]
