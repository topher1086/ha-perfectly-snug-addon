#! /usr/bin/with-contenv bashio
# CONFIG_PATH=/data/options.json

# export TOPPER_IP_ADDRESS="$(bashio::config 'topper_ip_address')"
# export SUPERVISOR_TOKEN="$(bashio::config 'SUPERVISOR_TOKEN')"

# echo "$SUPERVISOR_TOKEN"

# echo "Hello world!"
# printenv
# echo "${TOPPER_IP_ADDRESS}"

# python3 "./snuggler.py" "$SUPERVISOR_TOKEN"
python3 "./snuggler.py"