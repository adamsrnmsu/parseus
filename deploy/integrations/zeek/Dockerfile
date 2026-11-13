# Stock Zeek plus the Parseus Modbus/DNP3 script and a custom Spicy DNP3 analyzer.
# No zkg and no ICSNPP. The zeek/zeek image ships spicyz but no C++ compiler, so the
# analyzer is compiled in a throwaway builder stage and only the resulting .hlto is
# copied into the final image, which stays compiler-free.
#
# Offline builds: the final stage needs only the base image. The builder stage runs
# `apt-get install g++`, so building the image needs network access to the Debian
# mirrors once, or a registry/mirror that already holds a built parseus-zeek image
# (offline bundles ship the built image, so nothing is compiled at install time).
FROM zeek/zeek:9.0.1 AS builder
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends g++ make cmake \
 && rm -rf /var/lib/apt/lists/*
COPY spicy/ /src/spicy/
RUN spicyz -o /parseus_dnp3.hlto /src/spicy/parseus_dnp3.spicy /src/spicy/parseus_dnp3.evt

FROM zeek/zeek:9.0.1
COPY --from=builder /parseus_dnp3.hlto /opt/parseus/spicy/parseus_dnp3.hlto
COPY zeek/parseus.zeek /opt/parseus/zeek/parseus.zeek
COPY entrypoint.sh /opt/parseus/entrypoint.sh

RUN chmod 0755 /opt/parseus/entrypoint.sh && mkdir -p /zeek/logs
WORKDIR /zeek/logs
VOLUME /zeek/logs

ENTRYPOINT ["/opt/parseus/entrypoint.sh"]
