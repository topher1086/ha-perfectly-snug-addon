ARG BUILD_FROM
FROM $BUILD_FROM

# Copy data for add-on
COPY run.sh /
COPY snuggler.py /
COPY requirements.txt /
RUN chmod a+x /run.sh

RUN apk add --no-cache python3 py3-pip
RUN pip3 install -r requirements.txt

CMD ["./run.sh"]