# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:c2aca8207dfb68e7d20fecb7fad7e145c5d79d2ff36712d416c144130606d71d
ARG JRE_IMAGE=eclipse-temurin:25.0.5_8-jre-resolute@sha256:b6e00330337143a2c1a20f1c1c693d4b6ce9a3c30ade8b9d03fc68cc9e3871e7
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff

# -----------------------------------------------------------------------------
# Estágio JRE: Fornece o runtime Java 25 oficial do Eclipse Temurin (25.0.5+8, Ubuntu 26.04)
# -----------------------------------------------------------------------------
FROM ${JRE_IMAGE} AS jre-stage

# -----------------------------------------------------------------------------
# Estágio UV: Fornece os binários pré-compilados do uv
# -----------------------------------------------------------------------------
FROM ${UV_IMAGE} AS uv-stage

# -----------------------------------------------------------------------------
# Estágio 1: Download e validação criptográfica estrita dos binários Lavalink
# -----------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS lavalink-downloader

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/lavalink

ARG LL_BINARIES_URL="https://github.com/EmpireCommunity/EmpireMuse-LL-binaries/releases/download/0.0.5"
ARG LAVALINK_JAR_SHA256="8cb801e591072c3689fafd71ccf571a95a4ead3cc35dfc045e157d763d89119a"
ARG LAVALINK_APPLICATION_YML_SHA256="39ca3e7813e0db338535b0e098dd084ea22c278bb74a8ddeac92f1fc65f8bb46"
ARG YT_PLUGIN_NAME="youtube-plugin-2be8e542d3f6f178e048dca565892684c2e40177.jar"
ARG YT_PLUGIN_SHA256="b062effdda6abeaa46913dfd1b5d5600120b65f239ce88f30948c7213a3ca1c0"
ARG LAVASRC_PLUGIN_NAME="lavasrc-plugin-4.8.3.jar"
ARG LAVASRC_PLUGIN_SHA256="4ec389bdae64fd381ee62fcdb902e15d9402823b1ad0b982bd5633619a07c1b3"
ARG LAVALINK_FILTER_PLUGIN_NAME="lavalink-2.7.1.jar"
ARG LAVALINK_FILTER_PLUGIN_SHA256="4d63a711fa43f0f8ee88006e63b03d24c571ff54ea73d25cba83453b172cbf51"
ARG SOUNDY_PLUGIN_NAME="soundy-plugin-1.1.0.jar"
ARG SOUNDY_PLUGIN_SHA256="9568b5930969346b1afba89df8c0c6d373c2b553e087af09fb3c190f252a3cc9"

RUN mkdir -p /opt/lavalink/plugins && \
    curl --proto '=https' --tlsv1.2 -fSL --retry 3 "${LL_BINARIES_URL}/Lavalink.jar" -o /opt/lavalink/Lavalink.jar && \
    curl --proto '=https' --tlsv1.2 -fSL --retry 3 "${LL_BINARIES_URL}/application.yml" -o /opt/lavalink/application.yml && \
    curl --proto '=https' --tlsv1.2 -fSL --retry 3 "${LL_BINARIES_URL}/${YT_PLUGIN_NAME}" -o "/opt/lavalink/plugins/${YT_PLUGIN_NAME}" && \
    curl --proto '=https' --tlsv1.2 -fSL --retry 3 "${LL_BINARIES_URL}/${LAVASRC_PLUGIN_NAME}" -o "/opt/lavalink/plugins/${LAVASRC_PLUGIN_NAME}" && \
    curl --proto '=https' --tlsv1.2 -fSL --retry 3 "${LL_BINARIES_URL}/${LAVALINK_FILTER_PLUGIN_NAME}" -o "/opt/lavalink/plugins/${LAVALINK_FILTER_PLUGIN_NAME}" && \
    curl --proto '=https' --tlsv1.2 -fSL --retry 3 "${LL_BINARIES_URL}/${SOUNDY_PLUGIN_NAME}" -o "/opt/lavalink/plugins/${SOUNDY_PLUGIN_NAME}" && \
    printf "%s  /opt/lavalink/Lavalink.jar\n%s  /opt/lavalink/application.yml\n%s  /opt/lavalink/plugins/%s\n%s  /opt/lavalink/plugins/%s\n%s  /opt/lavalink/plugins/%s\n%s  /opt/lavalink/plugins/%s\n" \
      "${LAVALINK_JAR_SHA256}" \
      "${LAVALINK_APPLICATION_YML_SHA256}" \
      "${YT_PLUGIN_SHA256}" "${YT_PLUGIN_NAME}" \
      "${LAVASRC_PLUGIN_SHA256}" "${LAVASRC_PLUGIN_NAME}" \
      "${LAVALINK_FILTER_PLUGIN_SHA256}" "${LAVALINK_FILTER_PLUGIN_NAME}" \
      "${SOUNDY_PLUGIN_SHA256}" "${SOUNDY_PLUGIN_NAME}" \
      > /tmp/checksums.txt && \
    sha256sum -c /tmp/checksums.txt && \
    rm -f /tmp/checksums.txt && \
    chmod -R u=rwX,go=rX /opt/lavalink

# -----------------------------------------------------------------------------
# Estágio 2: Compilação e isolamento das dependências Python via uv
# -----------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS python-builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY --from=uv-stage /uv /uvx /bin/

# Sem pip no venv: quem instala é o uv, com o lock e os hashes, só neste estágio.
RUN python -m venv --without-pip /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements-lock.txt /tmp/
RUN uv pip install --python /opt/venv/bin/python --no-cache --require-hashes -r /tmp/requirements-lock.txt

# -----------------------------------------------------------------------------
# Estágio 3: Imagem final de runtime (não-root, enxuta, código somente leitura)
# -----------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.source="https://github.com/EmpireCommunity/EmpireMuse" \
      org.opencontainers.image.documentation="https://github.com/EmpireCommunity/EmpireMuse/blob/main/CREDITS.md" \
      org.opencontainers.image.licenses="GPL-2.0-only" \
      org.opencontainers.image.title="EmpireMuse" \
      org.opencontainers.image.authors="EmpireCommunity"

# Instalar tini do repositório Debian e ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Desconfiar das raízes emSign C1 e ECC C3, removidas do certifi 2026.10.23 (a "emSign Root CA - G1" segue confiável,
# como no certifi); remover quando o ca-certificates do Debian desconfiar delas. Os grep derrubam o build se um
# ca-certificates futuro renomear ou tirar as linhas (o repositório apt não é fixado): sem eles, o sed não trocaria nada e as
# raízes voltariam a ser confiáveis em silêncio. Se quebrar por isso, confira se o Debian já as desconfia e tire o bloco.
RUN sed -i \
      -e 's|^mozilla/emSign_Root_CA_-_C1.crt$|!&|' \
      -e 's|^mozilla/emSign_ECC_Root_CA_-_C3.crt$|!&|' \
      /etc/ca-certificates.conf && \
    grep -qxF "!mozilla/emSign_Root_CA_-_C1.crt" /etc/ca-certificates.conf && \
    grep -qxF "!mozilla/emSign_ECC_Root_CA_-_C3.crt" /etc/ca-certificates.conf && \
    update-ca-certificates --fresh

# Copiar JRE 25 oficial do Eclipse Temurin (apenas /opt/java/openjdk)
COPY --from=jre-stage /opt/java/openjdk /opt/java/openjdk

# O Temurin 25.0.5 já não confia, no cacerts da JRE, nas raízes emSign que o certifi travado não tem (C1, ECC C3 e ECC G3; só a
# "emSign Root CA - G1" fica, como no certifi). Este passo garante que continua assim, e falha fechado: a listagem do cacerts tem
# de funcionar (keytool, caminho e senha), a G1 tem de aparecer (prova que a listagem está completa) e nenhuma das três raízes pode
# aparecer. Confere as impressões digitais SHA-256, não os aliases, que podem ser renomeados. Se um Temurin futuro voltar a
# confiar nelas, remova-as com `keytool -delete -cacerts -storepass changeit -alias <alias>`.
# "changeit" é a senha pública e padrão do cacerts, não um segredo.
RUN set -e; \
    store="$(/opt/java/openjdk/bin/keytool -list -cacerts -storepass changeit)"; \
    printf '%s\n' "$store" | grep -qF "40:F6:AF:03:46:A9:9A:A1:CD:1D:55:5A:4E:9C:CE:62:C7:F9:63:46:03:EE:40:66:15:83:3D:C8:C8:D0:03:67" || \
      { echo "cacerts da JRE sem a emSign Root CA - G1: a listagem não é confiável." >&2; exit 1; }; \
    for fingerprint in \
      "12:56:09:AA:30:1D:A0:A2:49:B9:7A:82:39:CB:6A:34:21:6F:44:DC:AC:9F:39:54:B1:42:92:F2:E8:C8:60:8F" \
      "BC:4D:80:9B:15:18:9D:78:DB:3E:1D:8C:F4:F9:72:6A:79:5D:A1:64:3C:A5:F1:35:8E:1D:DB:0E:DC:0D:7E:B3" \
      "86:A1:EC:BA:08:9C:4A:8D:3B:BE:27:34:C6:12:BA:34:1D:81:3E:04:3C:F9:E8:A8:62:CD:5C:57:A3:6B:BE:6B"; do \
      if printf '%s\n' "$store" | grep -qF "$fingerprint"; then \
        echo "A JRE voltou a confiar numa raiz emSign ($fingerprint): remova-a do cacerts (keytool -delete)." >&2; exit 1; \
      fi; \
    done

# Copiar os binários do Lavalink validados
COPY --from=lavalink-downloader /opt/lavalink /opt/lavalink

# Copiar o ambiente virtual de dependências Python
COPY --from=python-builder /opt/venv /opt/venv

# Sem pip na imagem final: nada instala pacotes em runtime (as dependências vêm do lock, com hash, no estágio python-builder).
# Tira o pip do Python da imagem base e o ensurepip, que traz uma wheel do pip. O teste no fim derruba o build se o pip
# ainda puder ser importado, em qualquer dos dois interpretadores.
RUN rm -rf /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14 \
      /usr/local/lib/python3.14/site-packages/pip /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
      /usr/local/lib/python3.14/ensurepip && \
    ! /usr/local/bin/python3 -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pip') else 1)" && \
    ! /opt/venv/bin/python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pip') else 1)"

# Criar usuário não-root fixo com nologin e preparar diretórios de dados e código.
# /run/empiremuse-updater é o diretório do socket do proxy de atualização (docs/guides/auto-update.md): dono 10002 (o
# proxy), grupo 10001 (o bot) e modo 0750, então o bot só atravessa o diretório e escreve no socket, sem poder apagá-lo
# nem trocá-lo. O volume nomeado herda esse dono e modo na primeira montagem.
RUN groupadd -g 10001 empiremuse && \
    useradd -u 10001 -g empiremuse -m -d /home/empiremuse -s /usr/sbin/nologin empiremuse && \
    mkdir -p /data /app /run/empiremuse-updater && \
    chown -R empiremuse:empiremuse /data && \
    chown 10002:10001 /run/empiremuse-updater && \
    chmod 0750 /run/empiremuse-updater && \
    chmod -R u=rwX,go=rX /opt/lavalink

# Copiar os arquivos do código do bot como root (somente leitura para o usuário da app)
WORKDIR /app
COPY empiremuse/ ./empiremuse/
COPY main.py ./
COPY assets/ ./assets/
COPY data/ ./data/
COPY requirements.txt ./
COPY requirements-lock.txt ./
COPY LICENSE ./
COPY CREDITS.md ./
RUN chmod -R a+rX /app

# O runtime opera a partir do volume /data para que arquivos em cwd caiam em /data
WORKDIR /data

# Versão do bot dentro da imagem (X.Y.Z); o release.yml passa a da tag. "dev" é a imagem montada à mão, sem versão.
# Declarado aqui, depois dos RUN, para uma versão nova não invalidar o cache das camadas acima.
ARG EMPIREMUSE_VERSION=dev

# Variáveis de ambiente operacionais do container
ENV PATH="/opt/venv/bin:/opt/java/openjdk/bin:$PATH" \
    PYTHONPATH=/app \
    JAVA_HOME=/opt/java/openjdk \
    EMPIREMUSE_HOME=/data \
    RUN_LOCAL_LAVALINK=true \
    LAVALINK_OFFLINE=true \
    LAVALINK_JAVA_PATH=/opt/java/openjdk/bin/java \
    LAVALINK_HOME=/opt/lavalink \
    PORT=6000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EMPIREMUSE_VERSION=${EMPIREMUSE_VERSION} \
    EMPIREMUSE_HEALTH_FILE=/tmp/empiremuse.health \
    PYTHONSAFEPATH=1

USER 10001:10001
VOLUME ["/data"]
EXPOSE 6000

# Saúde por heartbeat: o bot só renova o arquivo com todos os bots prontos e, havendo nós, ao menos um disponível
# (empiremuse/health.py). O PYTHONSAFEPATH impede o "-m" de pôr o cwd (/data, gravável) no topo do sys.path.
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --start-interval=5s --retries=3 \
    CMD ["python", "-m", "empiremuse.health"]

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "empiremuse"]
