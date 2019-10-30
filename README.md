[TOC]

------

# MAAS

Official site - https://maas.io/

GitHub mirror - https://github.com/maas/maas [reference for branches 'master' and '2.4']

MAAS Region Controller consists of:

- REST API server (TCP port 5240)
- PostgreSQL database
- DNS
- caching HTTP proxy
- web UI

MAAS Rack Controller provides:

- DHCP
- TFTP
- HTTP (for images)
- power management





------

## Installing MAAS

MAAS **2.4.2**-7034-g2f5deb8b8-0ubuntu1 was installed above **Ubuntu 18.04** LTS running on HP ElitDesk 800  in the lab. While MAAS is available in the normal Ubuntu archives, the available  packages may be lagging behind non-archive, but still stable, versions. 

Older versions running on Ubuntu 16.04 are defective. 

In our case Region and Rack Controller are running on the same node.

Follow the official installation guide - [link](https://maas.io/install):

``````bash
sudo apt-add-repository -yu ppa:maas/stable
sudo apt update
sudo apt install maas
sudo maas init
``````



Generate SSH keys for the admin user (`fides` by default):

``````
 ssh-keygen -t rsa -b 4096 -C "$(whoami)@$(hostname)"
``````





------

## Configuring MAAS

Log in to the MAAS UI at `http://<your.maas.ip>:5240/MAAS/`  and complete the following configurations:

### Settings

- `http://<your.maas.ip>:5240/MAAS/#/settings/general/`
  - Region name: `scada-maas-rc`  for example
  - Commissioning: select the same OS as MAAS server (Ubuntu 18.04) and HWE kernel (hwe-18.04)
  - Deploy: will be updated later on, after importing the custom Debian OS image
- `http://<your.maas.ip>:5240/MAAS/#/settings/storage/`
  - Select 'Flat layout' in the dropd down menu
  - Select 'Use quick erase by default when erasing disks.' only
- `http://<your.maas.ip>:5240/MAAS/#/settings/network/`
  - Proxy:  MAAS Built-in 
  - DNS: 10.6.2.19 10.0.0.19 10.0.0.25 8.8.8.8
    Note: Use both `c4internal` and `cyberbit` domain controllers to be on the safe side, but always use some external DNS for backup.
- `http://<your.maas.ip>:5240/MAAS/#/settings/repositories/`
  - Enable both default Ubuntu repositories

### Networks

* `http://<your.maas.ip>:5240/MAAS/#/networks/`
  * Enable DHCP in `untagged` VLAN in order to allow PXE boot via DHCP broadcast. 

### Basic images for PXE

- `http://<your.maas.ip>:5240/MAAS/#/images/`
  - Ubuntu images (NOTE: `18.04` for `amd64` and `i386` are minimal prerequisites

### API Key

* `http://<your.maas.ip>:5240/MAAS/account/prefs/`  
  * Generate MAAS API key for your admin user by clicking "Generate MAAS key" in 
  * SSH keys for currently logged in user (generated in the previous step)

### Metrics

* Enable Prometheus metrics following the official guide - [link](https://maas.io/docs/prometheus-metrics)
* 





------

## Customizing MAAS

* Some configuration values are not available via UI or config files. If you need to adjust timeouts, you will have to edit `/usr/lib/python3/dist-packages/maasserver/node_status.py` - [link](src/maasserver/node_status.py#286)

* In order to enable Debian support for UEFI boot, the config template ` /usr/lib/python3/dist-packages/provisioningserver/templates/uefi/config.local.amd64.template` need to be adjusted - [link](../../maas/commits/b662dc514a575565f429f74ef3fa4a92b93860c7)





------

## Debian image

MAAS is not supporting Debian images out of the box, hence some work is needed to add a custom image:

``````bash
###########################################
# Downlead raw OS image
###########################################
DOWNLOAD_DIR=/tmp
cd $DOWNLOAD_DIR
wget https://cdimage.debian.org/cdimage/openstack/current-9/debian-9-openstack-amd64.raw

###########################################
# Mount OS image
###########################################
sudo mkdir /mnt/custom-os-loop
sudo mount -o ro,loop,offset=1048576,sync debian-9-openstack-amd64.raw

###########################################
# Implement grub and ufiboot workaround for Debian 9
###########################################
sudo chroot /mnt/custom-os-loop
sudo apt update
sudo apt install -y apt-transport-https ca-certificates efibootmgr xfsprogs
sudo apt-mark hold xfsprogs
echo "deb http://ftp.debian.org/debian buster main contrib non-free" >> /etc/apt/sources.list
sudo apt update
exit

###########################################
# Create image archive and unmount
###########################################
cd /mnt/custom-os-loop
IMAGE_ARCHIVE_NAME="debian-9-openstack-custom-amd64"
tar czvf $DOWNLOAD_DIR/$IMAGE_ARCHIVE_NAME.tgz .
sudo umount /mnt/custom-os-loop

###########################################
# Add new custoem image to MAAS
# NOTE: use 'name=custom/debian' only!!! 
#       MAAS parse the parameter in order to determine which EFI template to use.
###########################################
cd $DOWNLOAD_DIR
MAAS_USER="$(whoami)"
MAAS_API_KEY="enter-here-your-maas-api-key"
MAAS_API_SERVER="http://<your.maas.ip>:5240/MAAS"
maas login $MAAS_USER $MAAS_API_SERVER $MAAS_API_KEY
maas $MAAS_USER boot-resources create name=custom/debian title="$IMAGE_ARCHIVE_NAME" architecture=amd64/generic content@=$DOWNLOAD_DIR/$IMAGE_ARCHIVE_NAME.tgz
``````



------

## Curtin 

Following the naming convention, in order to map `curtin_userdata` for Debian to the `generic\debian` OS, you need to have the following file: `/etc/maas/preseeds/curtin_userdata_custom_amd64_generic_debian` - [link](../../maas/commits/8a217699e7f1cd705e86ac030ca7d0d6c1512f36)

**NOTE**: pay attention to `/boot/efi` partition !



------

## Adding Servers to MAAS

...



------

## REST API

### Authentication

OSS MAAS uses OAUTH v1 tokens for authentication. Once you obtain a token it will be in in following format: 

```php
/(?<oauth_consumer_key>^[a-zA-Z0-9]{18}):{1}(?<oauth_token>[a-zA-Z0-9]{18}):{1}(?<oauth_signature>[a-zA-Z0-9]{32})$/s
```

In order to call MAAS API endpoint, proper authentication header need to be set:

``````bash
TIMESTMAP=$(date +%s)
OAUTH_CONSUMER_KEY=$(echo $TOKEN | cut -f1 -d:)
OAUTH_TOKEN=$(echo $TOKEN | cut -f2 -d:)
OAUTH_SIGNATURE=$(echo $TOKEN | cut -f3 -d:)
OAUTH_NONCE=$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 11 | head -n 1)

# NOTE: piggyback a URL encoded & character before OAUTH_SIGNATURE
curl -X GET \
  http://<your.maas.ip>:5240/MAAS/api/2.0/<endpoint>/ \
  -H 'Accept: */*' \
  -H 'Accept-Encoding: gzip, deflate' \
  -H 'Authorization: OAuth oauth_consumer_key="$OAUTH_CONSUMER_KEY",oauth_token="$OAUTH_TOKEN",oauth_signature_method="PLAINTEXT",oauth_timestamp="1571129113",oauth_nonce="$OAUTH_NONCE",oauth_version="1.0",oauth_signature="%26$OAUTH_SIGNATURE"'
``````



### API Endpoints

See official documentation - [link](https://maas.io/docs/api)

[Postman](https://postman.co/) [Collection is WIP](https://documenter.getpostman.com/view/3756960/SVtYRmE8) and will be added to this repo later on



------

## Known issues

1. Custom Debian 9 OS image is polluted with Debian 10 sources that were necessary for grub\uefi\boot workaround. Host provisioning must take care of it!
2.  Since our HW varies a lot, Curtin cloud-init config is taking care only of a single disk `/dev/sda` .
3. Partition order need to be changed to allow root `/` partition resizing



------

## TODO