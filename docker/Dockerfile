FROM python:3.12.12-slim-bookworm
LABEL maintainer="yolo <justinyolo33@gmail.com>"
LABEL org.opencontainers.image.source=https://github.com/kingshot-project/Kingshot-Discord-Bot

COPY bootstrap.sh /bootstrap.sh

RUN set -x
RUN apt-get update && apt-get install --no-install-recommends -y curl ca-certificates unzip
RUN pip install --upgrade pip
RUN mkdir -p /app
# Download latest release directly from GitHub
RUN curl -sL $(curl -s https://api.github.com/repos/wacns/Kingshot-Discord-Bot-KinshotProject/releases/latest | grep "zipball_url" | cut -d '"' -f 4) -o /tmp/bot.zip \
    && unzip /tmp/bot.zip -d /tmp/bot \
    && mv /tmp/bot/*/* /app/ \
    && rm -rf /tmp/bot.zip /tmp/bot
RUN cd /app && pip install -r requirements.txt
RUN chmod +x /bootstrap.sh
RUN apt-get -y autoremove && apt-get -y autoclean && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

WORKDIR /app
ENTRYPOINT ["/bin/sh", "/bootstrap.sh"]