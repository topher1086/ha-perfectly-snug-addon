ARG BUILD_FROM
FROM ghcr.io/home-assistant/amd64-base:latest

# Copy data for add-on
COPY run.sh /
COPY snuggler.py /
COPY requirements.txt /
RUN chmod a+x /run.sh

RUN apk add --no-cache
RUN apk add python3
RUN apk add py3-pip

# Alpine 3.19 doesn't work without a virtual environment now
# set the environment variable and add it to the path so it works
ENV VIRTUAL_ENV=/.venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN pip3 install -r requirements.txt

CMD ["./run.sh"]