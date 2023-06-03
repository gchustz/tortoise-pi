#! /bin/bash

# See if we're in the right dir
cd ../tortoise-pi

cp tortoise_service.py /usr/bin/
cp switch-gpio /usr/bin

SYSTEMD_PATH=/usr/lib/systemd/system
cp tortoise_setup.service $SYSTEMD_PATH
cp tortoise_gpio.service $SYSTEMD_PATH
cp tortoise_gpio_cleanup.service $SYSTEMD_PATH

sudo systemctl daemon-reload
sudo systemctl enable tortoise_setup.service
sudo systemctl enable tortoise_gpio.service
sudo systemctl enable tortoise_gpio_cleanup.service

sudo grep -qxF "dtoverlay=w1-gpio" /boot/config.txt || sudo echo "dtoverlay=w1-gpio" >> /boot/config.txt

pip3 install -r requirements.txt

echo "Complete"
echo "Please reboot..."