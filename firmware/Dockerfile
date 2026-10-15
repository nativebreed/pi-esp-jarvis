# Builds and flashes xiaozhi-esp32 firmware for the Waveshare ESP32-S3-Touch-AMOLED-1.8.
# Uses Espressif's official ESP-IDF image (has a native 64-bit ARM build for the Pi 5).
FROM espressif/idf:release-v6.1

# Pinned so the build is reproducible. Change to build a newer version.
ARG XIAOZHI_REPO=https://github.com/78/xiaozhi-esp32.git
ARG XIAOZHI_REF=d7a1c05308fb6e116ffb44b9e2c6e67b3366302a

RUN git init -q /opt/xiaozhi \
 && cd /opt/xiaozhi \
 && git remote add origin "$XIAOZHI_REPO" \
 && git fetch -q --depth 1 origin "$XIAOZHI_REF" \
 && git checkout -q FETCH_HEAD

COPY fw.sh /usr/local/bin/fw.sh
COPY patch_firmware.py /opt/patch_firmware.py
COPY face/emoji /opt/face-emoji
RUN chmod +x /usr/local/bin/fw.sh

WORKDIR /opt/xiaozhi
ENTRYPOINT ["/usr/local/bin/fw.sh"]
