#!/bin/sh
# Dekaron G stub: make BlueStacks trust the stub CA (bind-mount over the read-only system store). Run from the PC:
#   adb -s 127.0.0.1:5555 push "D:\claudetest\dekaron-g\tools\stub\certs\7a4f252f.0" /data/local/tmp/7a4f252f.0
#   adb -s 127.0.0.1:5555 shell "su -c 'sh /data/local/tmp/install-ca.sh'"
mkdir -p /data/local/tmp/cacerts
cp /system/etc/security/cacerts/* /data/local/tmp/cacerts/
cp /data/local/tmp/7a4f252f.0 /data/local/tmp/cacerts/
chmod 644 /data/local/tmp/cacerts/*
chcon -R u:object_r:system_file:s0 /data/local/tmp/cacerts 2>/dev/null
mount -o bind /data/local/tmp/cacerts /system/etc/security/cacerts
ls /system/etc/security/cacerts | wc -l
ls -la /system/etc/security/cacerts/7a4f252f.0
