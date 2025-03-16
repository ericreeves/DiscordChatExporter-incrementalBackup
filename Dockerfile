FROM tyrrrz/discordchatexporter:stable

RUN apk add --no-cache python3 py3-pip

COPY backup.py /opt/app/

ENTRYPOINT [ "python3", "/opt/app/backup.py" ]

