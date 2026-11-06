# syntax=docker/dockerfile:1
# Windows-Installer für das Logotherm Gateway – vollständig im (Linux-)Container gebaut.
#
#   Stufe wdi:   wdi-simple.exe (libwdi, WinUSB) per MinGW-w64-Cross-Build
#   Stufe build: Windows-Python 3.12 + PyInstaller + Inno Setup unter Wine
#   Stufe out:   nur die Setup-Exe (für `docker build --output`)
#
# Aufruf (aus dem Repo-Root, am einfachsten über packaging\windows\build-docker.ps1):
#   docker build -f packaging/windows/docker/Dockerfile --target out --output type=local,dest=release .

# ---- Stufe 1: wdi-simple.exe ------------------------------------------------------------------
FROM debian:bookworm-slim AS wdi
ARG LIBWDI_TAG=v1.5.1
ARG WDK_REDIST_URL=https://go.microsoft.com/fwlink/p/?LinkID=253170
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git make autoconf automake libtool pkg-config gcc libc6-dev \
        gcc-mingw-w64-x86-64 binutils-mingw-w64-x86-64 mingw-w64-x86-64-dev msitools \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# WinUSB-Coinstaller aus dem WDK-Redist (wie im offiziellen libwdi-Build)
RUN curl -fsSL -o wdk-redist.msi "${WDK_REDIST_URL}" \
    && mkdir wdk && cd wdk && msiextract ../wdk-redist.msi >/dev/null \
    && find . -iname 'winusbcoinstaller2.dll' -o -iname 'WdfCoInstaller01011.dll'
RUN git clone --depth 1 --branch "${LIBWDI_TAG}" https://github.com/pbatard/libwdi.git
WORKDIR /build/libwdi
RUN F="$(find /build/wdk -ipath '*/redist/wdf/x64/WdfCoInstaller01011.dll' | head -1)" \
    && WDK="${F%/redist/*}" && echo "WDK_DIR=${WDK}" && test -n "$F" \
    && autoreconf -fi \
    && CC_FOR_BUILD=gcc ./configure --host=x86_64-w64-mingw32 --disable-32bit \
         --enable-examples-build --disable-debug --with-wdkdir="${WDK}" \
    # Beim Cross-Build setzt configure diese beiden nicht (AC_CHECK_FILES nur nativ) -> selbst setzen
    && COI=wdf && if [ -d "${WDK}/redist/winusb" ]; then COI=winusb; fi \
    && test -f "${WDK}/redist/${COI}/x64/winusbcoinstaller2.dll" \
    && printf '#define COINSTALLER_DIR "%s"\n#define X64_DIR "x64"\n' "${COI}" >> config.h \
    && make -j"$(nproc)" \
    && ls -l examples/*.exe \
    && mkdir -p /out && cp examples/wdi-simple.exe /out/

# ---- Stufe 2: PyInstaller + Inno Setup unter Wine ---------------------------------------------
FROM tobix/wine:stable AS build
ARG PYTHON_VERSION=3.12.10
ARG INNO_VERSION=6.7.4
ARG INNO_TAG=is-6_7_4
ARG WINSW_VERSION=2.12.0
ARG PYINSTALLER_VERSION=6.22.3
ENV WINEDEBUG=-all WINEPREFIX=/opt/wineprefix WINEARCH=win64 \
    WINEDLLOVERRIDES="winemenubuilder.exe,mscoree,mshtml=" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1

RUN xvfb-run sh -c "wineboot --init && wineserver -w"
RUN cd /tmp \
    && curl -fsSLO "https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-amd64.exe" \
    && curl -fsSLO "https://github.com/jrsoftware/issrc/releases/download/${INNO_TAG}/innosetup-${INNO_VERSION}.exe" \
    && xvfb-run sh -c "\
         wine python-${PYTHON_VERSION}-amd64.exe /quiet TargetDir=C:\\\\Python InstallAllUsers=1 \
              PrependPath=1 Include_doc=0 Include_test=0 Include_launcher=0 && \
         wine innosetup-${INNO_VERSION}.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- && \
         wineserver -w" \
    && rm -f /tmp/*.exe \
    && wine 'C:\Python\python.exe' --version \
    && test -f "${WINEPREFIX}/drive_c/Program Files (x86)/Inno Setup 6/ISCC.exe"

WORKDIR /src
# Abhängigkeiten zuerst (Docker-Cache): exakt requirements.lock (pytest wird per Spec ausgeschlossen)
COPY requirements.lock ./
RUN xvfb-run sh -c "wine 'C:\Python\python.exe' -m pip install --no-warn-script-location \
         -r requirements.lock pyinstaller==${PYINSTALLER_VERSION} && wineserver -w"
RUN curl -fsSL -o /tmp/WinSW-x64.exe \
      "https://github.com/winsw/winsw/releases/download/v${WINSW_VERSION}/WinSW-x64.exe"

COPY . .
COPY --from=wdi /out/wdi-simple.exe packaging/windows/driver/wdi-simple.exe
RUN sh packaging/windows/docker/build.sh

# ---- Stufe 3: Ergebnis ------------------------------------------------------------------------
FROM scratch AS out
COPY --from=build /src/dist/installer/ /
