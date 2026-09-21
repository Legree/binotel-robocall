#!/bin/sh
# Підставляє SIP-дані та секрет AMI з .env у конфіги — у git секретів немає.
set -e
sed -i "s/BINOTEL_SIP_HOST/${BINOTEL_SIP_HOST}/g; s/BINOTEL_SIP_LOGIN/${BINOTEL_SIP_LOGIN}/g; s/BINOTEL_SIP_PASSWORD/${BINOTEL_SIP_PASSWORD}/g" /etc/asterisk/pjsip.conf
sed -i "s/^secret=.*/secret=${AMI_SECRET}/" /etc/asterisk/manager.conf
chown -R asterisk:asterisk /var/lib/asterisk/sounds/robocall
exec asterisk -f -U asterisk -G asterisk -vvv
