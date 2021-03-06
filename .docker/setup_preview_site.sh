#!/bin/bash
if [ -z "$1" ]
then
        echo "Must specify the site URL as an argument"
        exit 1
fi

source /usr/local/bin/config_env
echo "Setting up preview site to $1"
cd $APP_DIR

python manage.py setup_current_site $1

