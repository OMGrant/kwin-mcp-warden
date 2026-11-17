# kwin-mcp-warden's boundary tests as another Linux user on another distro (deferred item 12).
#
# Built on upstream's E2E image (docker/e2e.Dockerfile, Debian trixie at a pinned snapshot):
# its KWin, its venv and its unprivileged user "tester". Adds what the warden draws with
# (Qt's qml program, Kirigami, layer-shell-qt, Qt Quick modules), KCharSelect (the second
# test app) and gdbus, then installs this checkout as a wheel, so the tests run against an
# installed package with a fresh home, never the source folder or the host's home.
# Build and run through tests/warden/container.sh.
ARG BASE=kwin-mcp-e2e
FROM ${BASE}

USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        kcharselect \
        layer-shell-qt \
        qml6-module-org-kde-layershell \
        libglib2.0-bin \
        qml-qt6 \
        qml6-module-org-kde-kirigami \
        qml6-module-qt-labs-platform \
        qml6-module-qtquick \
        qml6-module-qtquick-effects \
        qml6-module-qtquick-window \
        qml6-module-qtqml-workerscript \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=1000:1000 . /tmp/warden-src
RUN /opt/kwin-mcp-venv/bin/python -m pip install --no-deps --force-reinstall --quiet \
        /tmp/warden-src \
    && install -d -o tester -g tester /home/tester/boundary \
    && cp -r /tmp/warden-src/tests/warden /home/tester/boundary/ \
    && chown -R tester:tester /home/tester/boundary \
    && rm -rf /tmp/warden-src

USER tester
WORKDIR /home/tester
ENTRYPOINT []
CMD ["/opt/kwin-mcp-venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider", \
     "/home/tester/boundary/warden/test_boundaries.py"]
