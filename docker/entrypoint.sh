#!/bin/bash
# One image, two entrypoints (08 section 2). Which one you get is decided by whether the Lambda
# runtime is there to talk to - not by a second image, a second tag or an argument you have to
# remember.
#
#   in Lambda      $AWS_LAMBDA_RUNTIME_API is set  ->  the runtime client, CMD is the handler
#   anywhere else  it is not                       ->  run the arguments as a command
#
#     docker run IMAGE stratum plugins list
#     docker run IMAGE stratum exec --plan ... --stage regrid --index 0
set -euo pipefail

# shellcheck disable=SC1091
source /opt/stratum/activate.sh

if [ -n "${AWS_LAMBDA_RUNTIME_API:-}" ]; then
  exec python -m awslambdaric "$@"
fi

if [ "$#" -eq 0 ]; then
  exec stratum --help
fi

# A bare handler path with no runtime to serve it is the commonest way to run this image wrong,
# so say what happened rather than failing inside the runtime client.
case "$1" in
  *.handler|*:handler)
    echo "stratum: '$1' is the Lambda handler, but \$AWS_LAMBDA_RUNTIME_API is not set." >&2
    echo "         Run a command instead, e.g. 'stratum plugins list', or start this image" >&2
    echo "         under the Lambda Runtime Interface Emulator." >&2
    exit 64
    ;;
esac

exec "$@"
