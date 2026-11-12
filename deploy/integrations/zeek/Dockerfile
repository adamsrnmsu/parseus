# Stock Zeek plus the Parseus Modbus/DNP3 script. No zkg and no ICSNPP, so the
# image builds offline from the base image alone.
FROM zeek/zeek:9.0.1

COPY zeek/parseus.zeek /opt/parseus/zeek/parseus.zeek
COPY entrypoint.sh /opt/parseus/entrypoint.sh

RUN chmod 0755 /opt/parseus/entrypoint.sh && mkdir -p /zeek/logs
WORKDIR /zeek/logs
VOLUME /zeek/logs

ENTRYPOINT ["/opt/parseus/entrypoint.sh"]
