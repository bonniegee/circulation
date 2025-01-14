###############################################################################
## lcpencrypt
###############################################################################

FROM golang:1.17 AS builder

LABEL maintainer="The Palace Project <info@thepalaceproject.org>"

RUN go get -v github.com/readium/readium-lcp-server/lcpencrypt

###############################################################################
## Base Image
###############################################################################

FROM phusion/baseimage:focal-1.1.0 As base

COPY --from=builder /go/bin/lcpencrypt /go/bin/lcpencrypt

# Copy over all Palace build files for this image
COPY ./docker /ls_build
RUN /bin/bash -c "/ls_build/system_setup.sh"
COPY --chown=1000:1000 poetry.lock pyproject.toml /var/www/circulation/
RUN /bin/bash -c "/ls_build/app_setup.sh \
      && /ls_build/logrotate.sh"

COPY --chown=1000:1000 . /var/www/circulation

###############################################################################
## Circ-exec Image
###############################################################################

FROM base as exec

ENV SIMPLIFIED_DB_TASK "ignore"
ENV SIMPLIFIED_SCRIPT_NAME ""

RUN /bin/bash -c "rm -rf /ls_build && /bd_build/cleanup.sh"

VOLUME /var/log
WORKDIR /home/simplified/circulation/bin
CMD ["/sbin/my_init", "--skip-runit", "--quiet", "--", \
     "/bin/bash", "-c", \
     "source ../env/bin/activate && ./${SIMPLIFIED_SCRIPT_NAME}"]

###############################################################################
## Circ-scripts Image
###############################################################################

FROM base as scripts

ENV SIMPLIFIED_DB_TASK "auto"
# Set the local timezone in /docker/simplified_cron.sh
ENV TZ=US/Eastern

RUN /bin/bash -c "/ls_build/simplified_cron.sh \
      && rm -rf /ls_build && /bd_build/cleanup.sh"

VOLUME /var/log
WORKDIR /home/simplified/circulation/bin

CMD ["/sbin/my_init"]

###############################################################################
## Circ-webapp Image
###############################################################################

FROM base as webapp

ENV SIMPLIFIED_DB_TASK "ignore"

RUN /bin/bash -c "/ls_build/nginx.sh \
      && /ls_build/uwsgi.sh \
      && rm -rf /ls_build && /bd_build/cleanup.sh"

VOLUME /var/log
WORKDIR /home/simplified/circulation
EXPOSE 80

CMD ["/sbin/my_init"]
