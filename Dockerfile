FROM tyrrrz/discordchatexporter:stable

RUN apk add --no-cache python3 py3-pip

COPY backup.py /opt/app/

ENTRYPOINT [ "/opt/app/backup.py" ]

