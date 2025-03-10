# Copyright 2016-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""RPC helpers for getting the configuration for a booting machine."""

__all__ = [
    "get_config",
]

import re
import shlex

from django.core.exceptions import (
    ObjectDoesNotExist,
    ValidationError,
)
from maasserver.enum import (
    BOOT_RESOURCE_FILE_TYPE,
    INTERFACE_TYPE,
)
from maasserver.models import (
    BootResource,
    Config,
    Event,
    Node,
    PhysicalInterface,
    RackController,
    VLAN,
)
from maasserver.node_status import NODE_STATUS
from maasserver.preseed import (
    compose_enlistment_preseed_url,
    compose_preseed_url,
)
from maasserver.server_address import get_maas_facing_server_host
from maasserver.third_party_drivers import get_third_party_driver
from maasserver.utils.orm import transactional
from maasserver.utils.osystems import validate_hwe_kernel
from provisioningserver.events import EVENT_TYPES
from provisioningserver.rpc.exceptions import BootConfigNoResponse
from provisioningserver.utils.network import get_source_address
from provisioningserver.utils.twisted import (
    synchronous,
    undefined,
)


DEFAULT_ARCH = 'i386'


def get_node_from_mac_string(mac_string):
    """Get a Node object from a MAC address string.

    Returns a Node object or None if no node with the given MAC address exists.
    """
    if mac_string is None:
        return None
    node = Node.objects.filter(
        interface__type=INTERFACE_TYPE.PHYSICAL,
        interface__mac_address=mac_string)
    node = node.select_related('boot_interface', 'domain')
    return node.first()


def event_log_pxe_request(machine, purpose):
    """Log PXE request to machines's event log."""
    options = {
        'commissioning': "commissioning",
        'rescue': "rescue mode",
        'xinstall': "installation",
        'ephemeral': "ephemeral",
        'local': "local boot",
        'poweroff': "power off",
    }
    Event.objects.create_node_event(
        machine, event_type=EVENT_TYPES.NODE_PXE_REQUEST,
        event_description=options[purpose])


def get_boot_filenames(
        arch, subarch, osystem, series,
        commissioning_osystem=undefined,
        commissioning_distro_series=undefined):
    """Return the filenames of the kernel, initrd, and boot_dtb for the boot
    resource."""
    if subarch == 'generic':
        # MAAS doesn't store in the BootResource table what subarch is the
        # generic subarch so lookup what the generic subarch maps to.
        try:
            boot_resource_subarch = validate_hwe_kernel(
                subarch, None, "%s/%s" % (arch, subarch), osystem, series,
                commissioning_osystem=commissioning_osystem,
                commissioning_distro_series=commissioning_distro_series)
        except ValidationError:
            # It's possible that no kernel's exist at all for this arch,
            # subarch, osystem, series combination. In that case just fallback
            # to 'generic'.
            boot_resource_subarch = 'generic'
    else:
        boot_resource_subarch = subarch

    try:
        # Get the filename for the kernel, initrd, and boot_dtb the rack should
        # use when booting.
        boot_resource = BootResource.objects.get(
            architecture="%s/%s" % (arch, boot_resource_subarch),
            name="%s/%s" % (osystem, series))
        boot_resource_set = boot_resource.get_latest_complete_set()
        boot_resource_files = {
            bfile.filetype: bfile.filename
            for bfile in boot_resource_set.files.all()
        }
    except ObjectDoesNotExist:
        # If a filename can not be found return None to allow the rack to
        # figure out what todo.
        return None, None, None
    kernel = boot_resource_files.get(BOOT_RESOURCE_FILE_TYPE.BOOT_KERNEL)
    initrd = boot_resource_files.get(BOOT_RESOURCE_FILE_TYPE.BOOT_INITRD)
    boot_dtb = boot_resource_files.get(BOOT_RESOURCE_FILE_TYPE.BOOT_DTB)
    return kernel, initrd, boot_dtb


def merge_kparams_with_extra(kparams, extra_kernel_opts):
    if kparams is None or kparams == '':
        return extra_kernel_opts

    """
    This section will merge the kparams with the extra opts. Our goal is to
    start with what is in kparams and then look to extra_opts for anything
    to add to or override settings in kparams. Anything in extra_opts, which
    can be set through tabs, takes precedence so we use that to start with.
    """
    final_params = ''
    if extra_kernel_opts is not None and extra_kernel_opts != '':
        # We need to remove spaces from the tempita subsitutions so the split
        #  command works as desired.
        final_params = re.sub('{{\s*([\w\.]*)\s*}}', '{{\g<1>}}',
                              extra_kernel_opts)

    # Need to get a list of all kernel params in the extra opts.
    elist = []
    if len(final_params) > 0:
        tmp = shlex.split(final_params)
        for tparam in tmp:
            idx = tparam.find('=')
            key = tparam[0:idx]
            elist.append(key)

    # Go through all the kernel params as normally set.
    new_kparams = re.sub('{{\s*([\w\.]*)\s*}}', '{{\g<1>}}', kparams)
    params = shlex.split(new_kparams)
    for param in params:
        idx = param.find('=')
        key = param[0:idx]
        value = param[idx + 1:len(param)]

        # The call to split will remove quotes, so we add them back in if
        #  needed.
        if value.find(" ") > 0:
            value = '"' + value + '"'

        # if the param is not in extra_opts, use the one from here.
        if key not in elist:
            final_params = final_params + ' ' + key + '=' + value

    return final_params


def get_boot_config_for_machine(machine, configs, purpose):
    """Get the correct operating system and series based on the purpose
    of the booting machine.
    """
    _, subarch = machine.split_arch()
    precise = False
    if purpose == "commissioning":
        # LP: #1768321 - Fix a regression introduced by, and really fix
        # the issue that LP: #1730525 was meant to fix. This ensures that
        # when DISK_ERASING, or when ENTERING_RESCUE_MODE on a deployed
        # machine it uses the OS from the deployed system for the
        # ephemeral environment.
        if machine.osystem == "ubuntu" and (
                machine.status == NODE_STATUS.DISK_ERASING or (
                machine.status in [
                    NODE_STATUS.ENTERING_RESCUE_MODE,
                    NODE_STATUS.RESCUE_MODE,
                ] and machine.previous_status == NODE_STATUS.DEPLOYED)):
            osystem = machine.get_osystem(default=configs['default_osystem'])
            series = machine.get_distro_series(
                default=configs['default_distro_series'])
        else:
            osystem = configs['commissioning_osystem']
            series = configs['commissioning_distro_series']
    else:
        osystem = machine.get_osystem(default=configs['default_osystem'])
        series = machine.get_distro_series(
            default=configs['default_distro_series'])
        # XXX: roaksoax LP: #1739761 - Since the switch to squashfs (and
        # drop of iscsi), precise is no longer deployable. To address a
        # squashfs image is made available allowing it to be deployed in
        # the commissioning ephemeral environment.
        precise = True if series == "precise" else False
        if purpose == "xinstall" and (osystem != "ubuntu" or precise):
            # Use only the commissioning osystem and series, for operating
            # systems other than Ubuntu. As Ubuntu supports HWE kernels,
            # and needs to use that kernel to perform the installation.
            osystem = configs['commissioning_osystem']
            series = configs['commissioning_distro_series']

    # Pre MAAS-1.9 the subarchitecture defined any kernel the machine
    # needed to be able to boot. This could be a hardware enablement
    # kernel(e.g hwe-t) or something like highbank. With MAAS-1.9 any
    # hardware enablement kernel must be specifed in the hwe_kernel field,
    # any other kernel, such as highbank, is still specifed as a
    # subarchitecture. Since Ubuntu does not support architecture specific
    # hardware enablement kernels(i.e a highbank hwe-t kernel on precise)
    # we give precedence to any kernel defined in the subarchitecture field

    # If the machine is deployed, hardcode the use of the Minimum HWE Kernel
    # This is to ensure that machines can always do testing regardless of
    # what they were deployed with, using the defaults from the settings
    testing_from_deployed = (
        machine.previous_status == NODE_STATUS.DEPLOYED and
        machine.status == NODE_STATUS.TESTING and purpose == "commissioning")
    if testing_from_deployed:
        subarch = (subarch if not configs['default_min_hwe_kernel']
                   else configs['default_min_hwe_kernel'])
    # XXX: roaksoax LP: #1739761 - Do not override the subarch (used for
    # the deployment ephemeral env) when deploying precise, provided that
    # it uses the commissioning distro_series and hwe kernels are not
    # needed.
    elif subarch == "generic" and machine.hwe_kernel and not precise:
        subarch = machine.hwe_kernel
    elif(subarch == "generic" and
         purpose == "commissioning" and
         machine.min_hwe_kernel):
        try:
            subarch = validate_hwe_kernel(
                None, machine.min_hwe_kernel, machine.architecture,
                osystem, series)
        except ValidationError:
            subarch = "no-such-kernel"
    return osystem, series, subarch


@synchronous
@transactional
def get_config(
        system_id, local_ip, remote_ip, arch=None, subarch=None, mac=None,
        bios_boot_method=None):
    """Get the booting configration for the a machine.

    Returns a structure suitable for returning in the response
    for :py:class:`~provisioningserver.rpc.region.GetBootConfig`.

    Raises BootConfigNoResponse when booting machine should fail to next file.
    """
    rack_controller = RackController.objects.get(system_id=system_id)
    region_ip = None
    if remote_ip is not None:
        region_ip = get_source_address(remote_ip)
    machine = get_node_from_mac_string(mac)

    # Get the service address to the region for that given rack controller.
    server_host = get_maas_facing_server_host(
        rack_controller=rack_controller, default_region_ip=region_ip)

    # Fail with no response early so no extra work is performed.
    if machine is None and arch is None and mac is not None:
        # Request was pxelinux.cfg/01-<mac> for a machine MAAS does not know
        # about. So attempt fall back to pxelinux.cfg/default-<arch>-<subarch>
        # for arch detection.
        raise BootConfigNoResponse()

    configs = Config.objects.get_configs([
        'commissioning_osystem',
        'commissioning_distro_series',
        'enable_third_party_drivers',
        'default_min_hwe_kernel',
        'default_osystem',
        'default_distro_series',
        'kernel_opts',
    ])
    if machine is not None:
        # Update the last interface, last access cluster IP address, and
        # the last used BIOS boot method.
        if (machine.boot_interface is None or
                machine.boot_interface.mac_address != mac):
            machine.boot_interface = PhysicalInterface.objects.get(
                mac_address=mac)
        if (machine.boot_cluster_ip is None or
                machine.boot_cluster_ip != local_ip):
            machine.boot_cluster_ip = local_ip
        if machine.bios_boot_method != bios_boot_method:
            machine.bios_boot_method = bios_boot_method

        # Reset the machine's status_expires whenever the boot_config is called
        # on a known machine. This allows a machine to take up to the maximum
        # timeout status to POST.
        machine.reset_status_expires()

        # Does nothing if the machine hasn't changed.
        machine.save()

        # Update the VLAN of the boot interface to be the same VLAN for the
        # interface on the rack controller that the machine communicated with,
        # unless the VLAN is being relayed.
        rack_interface = rack_controller.interface_set.filter(
            ip_addresses__ip=local_ip).select_related('vlan').first()
        if (rack_interface is not None and
                machine.boot_interface.vlan_id != rack_interface.vlan_id):
            # Rack controller and machine is not on the same VLAN, with DHCP
            # relay this is possible. Lets ensure that the VLAN on the
            # interface is setup to relay through the identified VLAN.
            if not VLAN.objects.filter(
                    id=machine.boot_interface.vlan_id,
                    relay_vlan=rack_interface.vlan_id).exists():
                # DHCP relay is not being performed for that VLAN. Set the VLAN
                # to the VLAN of the rack controller.
                machine.boot_interface.vlan = rack_interface.vlan
                machine.boot_interface.save()

        arch, subarch = machine.split_arch()
        preseed_url = compose_preseed_url(
            machine, rack_controller, default_region_ip=region_ip)
        hostname = machine.hostname
        domain = machine.domain.name
        purpose = machine.get_boot_purpose()

        # Early out if the machine is booting local.
        if purpose == 'local':
            return {
                "system_id": machine.system_id,
                "arch": arch,
                "subarch": subarch,
                "osystem": machine.osystem,
                "release": machine.distro_series,
                "kernel": '',
                "initrd": '',
                "boot_dtb": '',
                "purpose": purpose,
                "hostname": hostname,
                "domain": domain,
                "preseed_url": preseed_url,
                "fs_host": local_ip,
                "log_host": server_host,
                "extra_opts": '',
                "http_boot": True,
            }

        # Log the request into the event log for that machine.
        if (machine.status in [
                NODE_STATUS.ENTERING_RESCUE_MODE,
                NODE_STATUS.RESCUE_MODE] and purpose == 'commissioning'):
            event_log_pxe_request(machine, 'rescue')
        else:
            event_log_pxe_request(machine, purpose)

        osystem, series, subarch = get_boot_config_for_machine(
            machine, configs, purpose)

        # We don't care if the kernel opts is from the global setting or a tag,
        # just get the options
        _, effective_kernel_opts = machine.get_effective_kernel_options(
            default_kernel_opts=configs['kernel_opts'])

        # Add any extra options from a third party driver.
        use_driver = configs['enable_third_party_drivers']
        if use_driver:
            driver = get_third_party_driver(machine)
            driver_kernel_opts = driver.get('kernel_opts', '')

            combined_opts = ('%s %s' % (
                '' if effective_kernel_opts is None else effective_kernel_opts,
                driver_kernel_opts)).strip()
            if len(combined_opts):
                extra_kernel_opts = combined_opts
            else:
                extra_kernel_opts = None
        else:
            extra_kernel_opts = effective_kernel_opts

        kparams = BootResource.objects.get_kparams_for_node(
            machine, default_osystem=configs['default_osystem'],
            default_distro_series=configs['default_distro_series'])
        extra_kernel_opts = merge_kparams_with_extra(kparams,
                                                     extra_kernel_opts)
    else:
        purpose = "commissioning"  # enlistment
        preseed_url = compose_enlistment_preseed_url(
            rack_controller, default_region_ip=region_ip)
        hostname = 'maas-enlist'
        domain = 'local'
        osystem = configs['commissioning_osystem']
        series = configs['commissioning_distro_series']
        min_hwe_kernel = configs['default_min_hwe_kernel']

        # When no architecture is defined for the enlisting machine select
        # the best boot resource for the operating system and series. If
        # none exists fallback to the default architecture. LP #1181334
        if arch is None:
            resource = (
                BootResource.objects.get_default_commissioning_resource(
                    osystem, series))
            if resource is None:
                arch = DEFAULT_ARCH
            else:
                arch, _ = resource.split_arch()
        # The subarch defines what kernel is booted. With MAAS 2.1 this changed
        # from hwe-<letter> to hwe-<version> or ga-<version>. Validation
        # converts between the two formats to make sure a bootable subarch is
        # selected.
        if subarch is None:
            min_hwe_kernel = validate_hwe_kernel(
                None, min_hwe_kernel, '%s/generic' % arch, osystem, series)
        else:
            min_hwe_kernel = validate_hwe_kernel(
                None, min_hwe_kernel, '%s/%s' % (arch, subarch), osystem,
                series)
        # If no hwe_kernel was found set the subarch to the default, 'generic.'
        if min_hwe_kernel is None:
            subarch = 'generic'
        else:
            subarch = min_hwe_kernel

        # Global kernel options for enlistment.
        extra_kernel_opts = configs["kernel_opts"]

    # Set the final boot purpose.
    if machine is None and arch == DEFAULT_ARCH:
        # If the machine is enlisting and the arch is the default arch (i386),
        # use the dedicated enlistment template which performs architecture
        # detection.
        boot_purpose = "enlist"
    elif purpose == 'poweroff':
        # In order to power the machine off, we need to get it booted in the
        # commissioning environment and issue a `poweroff` command.
        boot_purpose = 'commissioning'
    else:
        boot_purpose = purpose

    kernel, initrd, boot_dtb = get_boot_filenames(
        arch, subarch, osystem, series,
        commissioning_osystem=configs['commissioning_osystem'],
        commissioning_distro_series=configs['commissioning_distro_series'])

    # Return the params to the rack controller. Include the system_id only
    # if the machine was known.
    params = {
        "arch": arch,
        "subarch": subarch,
        "osystem": osystem,
        "release": series,
        "kernel": kernel,
        "initrd": initrd,
        "boot_dtb": boot_dtb,
        "purpose": boot_purpose,
        "hostname": hostname,
        "domain": domain,
        "preseed_url": preseed_url,
        "fs_host": local_ip,
        "log_host": server_host,
        "extra_opts": '' if extra_kernel_opts is None else extra_kernel_opts,
        # As of MAAS 2.4 only HTTP boot is supported. This ensures MAAS 2.3
        # rack controllers use HTTP boot as well.
        "http_boot": True,
    }
    if machine is not None:
        params["system_id"] = machine.system_id
    return params
