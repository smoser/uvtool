#!/bin/sh
set -e

## Not waiting for runlevel 2 as this seems to work still
## on trusty, and snappy shows runlevel 5. Admittedly, don't know what
## problem this solved.
# Wait for runlevel 2
#while [ "$(runlevel|awk '{print $2}')" != 2 ]; do sleep $UVTOOL_WAIT_INTERVAL; done

# Wait for cloud-init's signal
while [ ! -e /var/lib/cloud/instance/boot-finished ]; do sleep $UVTOOL_WAIT_INTERVAL; done
