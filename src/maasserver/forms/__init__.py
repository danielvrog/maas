# Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Forms."""

__all__ = [
    "AddPartitionForm",
    "AdminMachineForm",
    "AdminMachineWithMACAddressesForm",
    "AdminNodeForm",
    "BootResourceForm",
    "BootResourceNoContentForm",
    "BootSourceForm",
    "BootSourceSelectionForm",
    "BulkNodeActionForm",
    "ClaimIPForm",
    "ClaimIPForMACForm",
    "CommissioningForm",
    "ConfigForm",
    "ControllerForm",
    "CreateBcacheForm",
    "CreateCacheSetForm",
    "CreatePhysicalBlockDeviceForm",
    "CreateLogicalVolumeForm",
    "CreateRaidForm",
    "CreateVolumeGroupForm",
    "DeleteUserForm",
    "DeployForm",
    "DeviceForm",
    "DeviceWithMACsForm",
    "EditUserForm",
    "FormatBlockDeviceForm",
    "FormatPartitionForm",
    "get_machine_create_form",
    "get_machine_edit_form",
    "get_node_edit_form",
    "GlobalKernelOptsForm",
    "KeyForm",
    "LicenseKeyForm",
    "list_all_usable_architectures",
    "MAASForm",
    "MachineForm",
    "NetworksListingForm",
    "NewUserCreationForm",
    "NetworkDiscoveryForm",
    "NodeForm",
    "MachineWithMACAddressesForm",
    "MachineWithPowerAndMACAddressesForm",
    "ProfileForm",
    "ReleaseIPForm",
    "ResourcePoolForm",
    "SSHKeyForm",
    "SSLKeyForm",
    "StorageSettingsForm",
    "TagForm",
    "ThirdPartyDriversForm",
    "UbuntuForm",
    "UpdateBcacheForm",
    "UpdateCacheSetForm",
    "UpdateDeployedPhysicalBlockDeviceForm",
    "UpdatePhysicalBlockDeviceForm",
    "UpdateRaidForm",
    "UpdateVirtualBlockDeviceForm",
    "UpdateVolumeGroupForm",
    "WindowsForm",
    "ZoneForm",
    ]

from collections import Counter
from functools import partial
from itertools import chain
import json
import re

from django import forms
from django.contrib.auth.forms import (
    UserChangeForm,
    UserCreationForm,
)
from django.contrib.auth.models import User
from django.core.exceptions import (
    NON_FIELD_ERRORS,
    ValidationError,
)
from django.core.validators import (
    MaxValueValidator,
    MinValueValidator,
)
from django.forms import (
    CheckboxInput,
    Form,
    MultipleChoiceField,
)
from django.http import QueryDict
from django.utils.safestring import mark_safe
from lxml import etree
from maasserver.api.utils import get_overridden_query_dict
from maasserver.audit import create_audit_event
from maasserver.clusterrpc.driver_parameters import (
    get_driver_choices,
    get_driver_parameters,
    get_driver_types,
)
from maasserver.clusterrpc.osystems import (
    gen_all_known_operating_systems,
    validate_license_key,
)
from maasserver.config_forms import SKIP_CHECK_NAME
from maasserver.enum import (
    BOOT_RESOURCE_FILE_TYPE,
    BOOT_RESOURCE_TYPE,
    CACHE_MODE_TYPE_CHOICES,
    FILESYSTEM_FORMAT_TYPE_CHOICES,
    FILESYSTEM_FORMAT_TYPE_CHOICES_DICT,
    FILESYSTEM_GROUP_RAID_TYPE_CHOICES,
    FILESYSTEM_TYPE,
    INTERFACE_TYPE,
    NODE_TYPE,
)
from maasserver.exceptions import NodeActionError
from maasserver.fields import (
    LargeObjectFile,
    MACAddressFormField,
    UnstrippedCharField,
)
from maasserver.forms.settings import (
    CONFIG_ITEMS_KEYS,
    get_config_field,
    INVALID_SETTING_MSG_TEMPLATE,
    validate_missing_boot_images,
)
from maasserver.models import (
    Bcache,
    BlockDevice,
    BootResource,
    BootResourceFile,
    BootResourceSet,
    BootSource,
    BootSourceCache,
    BootSourceSelection,
    CacheSet,
    Config,
    Controller,
    Device,
    Domain,
    Filesystem,
    Interface,
    LargeFile,
    LicenseKey,
    Machine,
    Node,
    PackageRepository,
    Partition,
    PartitionTable,
    PhysicalBlockDevice,
    RAID,
    ResourcePool,
    SSHKey,
    SSLKey,
    Tag,
    VirtualBlockDevice,
    VolumeGroup,
    Zone,
)
from maasserver.models.blockdevice import MIN_BLOCK_DEVICE_SIZE
from maasserver.models.partition import MIN_PARTITION_SIZE
from maasserver.node_action import (
    ACTION_CLASSES,
    ACTIONS_DICT,
)
from maasserver.utils.converters import machine_readable_bytes
from maasserver.utils.forms import (
    compose_invalid_choice_text,
    get_QueryDict,
    set_form_error,
)
from maasserver.utils.orm import (
    get_one,
    transactional,
)
from maasserver.utils.osystems import (
    get_distro_series_initial,
    get_release_requires_key,
    list_all_releases_requiring_keys,
    list_all_usable_osystems,
    list_all_usable_releases,
    list_osystem_choices,
    list_release_choices,
    validate_hwe_kernel,
    validate_min_hwe_kernel,
)
from maasserver.utils.threads import deferToDatabase
from netaddr import (
    IPNetwork,
    valid_ipv6,
)
from provisioningserver.events import EVENT_TYPES
from provisioningserver.logger import get_maas_logger
from provisioningserver.utils.network import make_network
from provisioningserver.utils.twisted import (
    asynchronous,
    FOREVER,
)
from twisted.internet.defer import DeferredList
from twisted.internet.task import coiterate
from twisted.python.failure import Failure


maaslog = get_maas_logger()

# A reusable null-option for choice fields.
BLANK_CHOICE = ('', '-------')


def _make_network_from_subnet(ip, subnet):
    return make_network(ip, IPNetwork(subnet.cidr).netmask)


def remove_None_values(data):
    """Return a new dictionary without the keys corresponding to None values.
    """
    return {key: value for key, value in data.items() if value is not None}


class APIEditMixin:
    """A mixin that allows sane usage of Django's form machinery via the API.

    First it ensures that missing fields are not errors, then it removes
    None values from cleaned data. This means that missing fields result
    in no change instead of an error.

    :ivar submitted_data: The `data` as originally submitted.
    """

    def full_clean(self):
        """For missing fields, default to the model's existing value."""
        self.submitted_data = self.data
        self.data = get_overridden_query_dict(
            self.initial, self.data, self.fields)
        super(APIEditMixin, self).full_clean()

    def _post_clean(self):
        """Override Django's private hook _post_save to remove None values
        from 'self.cleaned_data'.

        _post_clean is where the fields of the instance get set with the data
        from self.cleaned_data.  That's why the cleanup needs to happen right
        before that.
        """
        self.cleaned_data = remove_None_values(self.cleaned_data)
        super(APIEditMixin, self)._post_clean()


class WithPowerTypeMixin:
    """A form mixin which adds the correct power_type and power_parameter
    fields. This mixin overrides the 'save' method to persist
    these fields and is intended to be used with a class inheriting from
    ModelForm.
    """

    def __init__(self, *args, **kwargs):
        super(WithPowerTypeMixin, self).__init__(*args, **kwargs)
        self.data = self.data.copy()
        WithPowerTypeMixin.set_up_power_fields(self, self.data)

    @staticmethod
    def _get_power_type_value(form, data, machine):
        if data is None:
            data = {}
        value = data.get('power_type', form.initial.get('power_type'))

        # If value is None (this is a machine creation form or this
        # form deals with an API call which does not change the value of
        # field) or invalid: get the machine's current 'power_type'
        # value or the default value if this form is not linked to a machine.
        driver_types = get_driver_types(ignore_errors=True)
        if len(driver_types) == 0:
            return ''

        if value not in driver_types:
            return '' if machine is None else machine.power_type
        return value

    @staticmethod
    def _get_power_parameters(form, data, machine):
        if data is None:
            data = {}

        params_field_name = 'power_parameters'
        parameters = data.get(
            params_field_name, form.initial.get(params_field_name, {}))

        if isinstance(parameters, str):
            if parameters.strip() == '':
                parameters = {}
            else:
                try:
                    parameters = json.loads(parameters)
                except json.JSONDecodeError:
                    raise ValidationError(
                        "Failed to parse JSON %s" % params_field_name)

        # Integrate the machines existing parameters if unset by form.
        if machine:
            for key, value in machine.power_parameters.items():
                if parameters.get(key) is None:
                    parameters[key] = value
        return parameters

    @staticmethod
    def set_up_power_fields(form, data, machine=None, type_required=False):
        """Set up the correct fields.

        This can't be done at the model level because the choices need to
        be generated on the fly by get_driver_choices().
        """
        type_field_name = 'power_type'
        params_field_name = 'power_parameters'
        type_value = WithPowerTypeMixin._get_power_type_value(
            form, data, machine)
        choices = [BLANK_CHOICE] + get_driver_choices()
        form.fields[type_field_name] = forms.ChoiceField(
            required=type_required, choices=choices, initial=type_value)
        parameters = WithPowerTypeMixin._get_power_parameters(
            form, data, machine)
        skip_check = (
            form.data.get('%s_%s' % (
                params_field_name, SKIP_CHECK_NAME)) == 'true')
        form.fields[params_field_name] = (
            get_driver_parameters(
                parameters, skip_check=skip_check)[type_value])
        if form.instance is not None:
            if form.instance.power_type != '':
                form.initial[type_field_name] = form.instance.power_type
            if form.instance.power_parameters != '':
                for key, value in parameters.items():
                    form.initial['%s_%s' % (params_field_name, key)] = value

    @staticmethod
    def check_driver(form, cleaned_data):
        # skip_check tells us to allow parameters to be saved
        # without any validation.  Nobody can remember why this was
        # added at this stage but it might have been a request from
        # smoser, we think.
        type_field_name = 'power_type'
        params_field_name = 'power_parameters'
        skip_check = (
            form.data.get('%s_%s' % (
                params_field_name, SKIP_CHECK_NAME)) == 'true')
        # Try to contact the cluster controller; if it's down then we
        # prevent saving the form as we can't validate the power
        # parameters and type.
        if not skip_check:
            driver_types = get_driver_types(ignore_errors=True)
            if len(driver_types) == 0:
                set_form_error(
                    form, type_field_name,
                    "No rack controllers are connected, unable to validate.")

            # If type is not set and parameters skip_check is not
            # on, reset parameters (set it to the empty string).
            type_value = cleaned_data.get(type_field_name, '')
            if type_value == '':
                cleaned_data[params_field_name] = {}
        return cleaned_data

    @staticmethod
    def set_values(form, machine):
        """Set values onto the machine."""
        # Only change type if the type was passed in the initial
        # data. clean_data will always have type, so we cannot use that
        # as a reference.
        type_field_name = 'power_type'
        params_field_name = 'power_parameters'
        type_changed = False
        if form.data.get(type_field_name) is not None:
            new_type = form.cleaned_data.get(type_field_name)
            if machine.power_type != new_type:
                machine.power_type = new_type
                type_changed = True
        # Only change parameters if the parameters was passed in
        # the initial data. clean_data will always have parameters, so we
        # cannot use that as a reference.
        initial_parameters = {
            param
            for param in form.data.keys()
            if (param.startswith(params_field_name) and
                not param == '%s_%s' % (params_field_name, SKIP_CHECK_NAME))
        }
        if type_changed or len(initial_parameters) > 0:
            machine.power_parameters = form.cleaned_data.get(
                params_field_name)

    def clean(self):
        cleaned_data = super(WithPowerTypeMixin, self).clean()
        return WithPowerTypeMixin.check_driver(self, cleaned_data)

    def save(self, *args, **kwargs):
        """Persist the node into the database."""
        node = super(WithPowerTypeMixin, self).save()
        WithPowerTypeMixin.set_values(self, node)
        node.save()
        return node


class MAASModelForm(APIEditMixin, forms.ModelForm):
    """A form for editing models, with MAAS-specific behaviour.

    Specifically, it is much like Django's ``ModelForm``, but removes
    ``None`` values from cleaned data. This allows the forms to be used
    for both the UI and the API with unsuprising behaviour in both.

    With some fields (like boolean fields), the behavior of a UI-submitted
    form and a API-submitted form needs to be different: the UI will omit
    the field to denote "false" where the API will provide the existing
    value for the field.

    Each form needs to deal with this but this base class provides built-in
    support for marking that a form is used in the UI by passing a
    'ui_submission=True' parameter; this information can then be used by the
    form to specialize its behavior depending on whether the submission is
    made from the API or the UI.
    """

    def __init__(self, data=None, files=None, ui_submission=False, **kwargs):
        super(MAASModelForm, self).__init__(data=data, files=files, **kwargs)
        self.is_update = bool(kwargs.get('instance', None))
        if ui_submission:
            # Add the ui_submission field.  Insert it before the other fields,
            # so that the field validators will have access to it regardless of
            # whether their fields were defined before or after this one.
            ui_submission_field = (
                'ui_submission',
                forms.CharField(widget=forms.HiddenInput(), required=False),
                )
            # Django 1.6 and earlier use their own SortedDict class; 1.7 uses
            # the standard library's OrderedDict.  The differences are
            # deprecated in 1.6, but to be on the safe side we'll use whichever
            # class is actually being used.
            dict_type = self.fields.__class__
            self.fields = dict_type(
                [ui_submission_field] + list(self.fields.items()))

    def _update_errors(self, errors):
        """Provide Django 1.11-like behaviour in 1.8 as well."""
        if hasattr(errors, 'error_dict'):
            error_dict = errors
        else:
            error_dict = ValidationError({NON_FIELD_ERRORS: errors})
        super(MAASModelForm, self)._update_errors(error_dict)


def list_all_usable_architectures():
    """Return all architectures that can be used for nodes.

    These are the architectures for which any boot resource exists. Now that
    all clusters sync from the region, all cluster support the same
    architectures.
    """
    return sorted(BootResource.objects.get_usable_architectures())


def list_architecture_choices(architectures):
    """Return Django "choices" list for `architectures`."""
    # We simply return (name, name) as the choice for each architecture
    # here. We could do something more complicated to get a "nice"
    # label, but the truth is that architecture names are plenty
    # readable already.
    return [(arch, arch) for arch in architectures]


def pick_default_architecture(all_architectures):
    """Choose a default architecture, given a list of all usable ones.
    """
    if len(all_architectures) == 0:
        # Nothing we can do.
        return ''

    global_default = 'i386/generic'
    if global_default in all_architectures:
        # Generally, prefer basic i386.  It covers the most cases.
        return global_default
    else:
        # Failing that, just pick the first.
        return all_architectures[0]


def clean_distro_series_field(form, field, os_field):
    """Cleans the distro_series field in the form. Validating that
    the selected operating system matches the distro_series.

    :param form: `Form` class
    :param field: distro_series field name
    :param os_field: osystem field name
    :return: clean distro_series field value
    """
    new_distro_series = form.cleaned_data.get(field)
    if '*' in new_distro_series:
        new_distro_series = new_distro_series.replace('*', '')
    if new_distro_series is None or '/' not in new_distro_series:
        return new_distro_series
    os, release = new_distro_series.split('/', 1)
    if os_field in form.cleaned_data:
        new_os = form.cleaned_data[os_field]
        if os != new_os:
            raise ValidationError(
                "%s in %s does not match with "
                "operating system %s" % (release, field, os))
    return release


def find_osystem_and_release_from_release_name(name):
    """Return os and release for the given release name."""
    osystems = list_all_usable_osystems()
    possible_short_names = []
    for osystem in osystems:
        for release in osystem['releases']:
            if release['name'] == name:
                return osystem, release
            elif osystem['name'] == name:
                # If the given release matches the osystem name add it to
                # our list of possibilities. This allows a user to specify
                # Ubuntu and get the latest release available.
                possible_short_names.append({
                    'osystem': osystem,
                    'release': release})
            elif (osystem['name'] != "ubuntu" and
                  release['name'].startswith(name)):
                # Check if the given name is a shortened version of a known
                # name, e.g. centos7 for centos70.  We don't allow short names
                # for Ubuntu releases
                possible_short_names.append({
                    'osystem': osystem,
                    'release': release})
    if len(possible_short_names) > 0:
        # Do a reverse sort of all the possibilities and pick the top one.
        # This allows a user to do a short hand with versioning to pick the
        # latest release, e.g. we have centos70, centos71 given centos7 this
        # will pick centos71
        sorted_list = sorted(
            possible_short_names,
            key=lambda os_release: os_release['release']['name'],
            reverse=True)
        return sorted_list[0]['osystem'], sorted_list[0]['release']
    return None, None


def contains_managed_ipv6_interface(interfaces):
    """Does any of a list of cluster interfaces manage a IPv6 subnet?"""
    return any(
        interface.manages_static_range() and valid_ipv6(interface.ip)
        for interface in interfaces
        )


class CheckboxInputTrueDefault(CheckboxInput):
    """A CheckboxInput widget with 'True' as its default value.

    The default CheckboxInput assumes its default is 'False'.  This isn't
    a problem when the widget is used in the UI because the underlying model's
    "default" can override this but since we're using this widget to handle
    API's requests as well, we need to work around this limitation.
    """
    def value_from_datadict(self, data, files, name):
        if name not in data:
            return True
        else:
            return super(CheckboxInput, self).value_from_datadict(
                data, files, name)


class NodeForm(MAASModelForm):
    def __init__(self, request=None, *args, **kwargs):
        super(NodeForm, self).__init__(*args, **kwargs)

        # Even though it doesn't need it and doesn't use it, this form accepts
        # a parameter named 'request' because it is used interchangingly
        # with AdminMachineForm which actually uses this parameter.
        instance = kwargs.get('instance')
        if instance is None or instance.owner is None:
            self.has_owner = False
        else:
            self.has_owner = True

        # Are we creating a new node object?
        self.new_node = (instance is None)

    def clean_disable_ipv4(self):
        # Boolean fields only show up in UI form submissions as "true" (if the
        # box was checked) or not at all (if the box was not checked).  This
        # is different from API submissions which can submit "false" values.
        # Our forms are rigged to interpret missing fields as unchanged, but
        # that doesn't work for the UI.  A form in the UI always submits all
        # its fields, so in that case, no value means False.
        #
        # To kludge around this, the UI form submits a hidden input field named
        # "ui_submission" that doesn't exist in the API.  If this field is
        # present, go with the UI-style behaviour.
        form_data = self.submitted_data
        if 'ui_submission' in form_data and 'disable_ipv4' not in form_data:
            self.cleaned_data['disable_ipv4'] = False
        if self.cleaned_data.get('disable_ipv4'):
            raise ValidationError(
                'If specified, disable_ipv4 must be False.')
        return self.cleaned_data['disable_ipv4']

    def clean_swap_size(self):
        """Validates the swap size field and parses integers suffixed with K,
        M, G and T
        """
        swap_size = self.cleaned_data.get('swap_size')
        # XXX: ValueError -- arising from int(...) -- is handled only when
        # swap_size has no suffix. It should be handled, and ValidationError
        # raised in its place, regardless of suffix.
        if swap_size == '':
            return None
        elif swap_size.endswith('K'):
            return int(swap_size[:-1]) * 1000
        elif swap_size.endswith('M'):
            return int(swap_size[:-1]) * 1000000
        elif swap_size.endswith('G'):
            return int(swap_size[:-1]) * 1000000000
        elif swap_size.endswith('T'):
            return int(swap_size[:-1]) * 1000000000000
        try:
            return int(swap_size)
        except ValueError:
            raise ValidationError('Invalid size for swap: %s' % swap_size)

    def clean_domain(self):
        domain = self.cleaned_data.get('domain')
        if not domain:
            return None
        try:
            return Domain.objects.get(id=int(domain))
        except ValueError:
            try:
                return Domain.objects.get(name=domain)
            except Domain.DoesNotExist:
                raise ValidationError("Unable to find domain %s" % domain)

    hostname = forms.CharField(
        label="Host name", required=False, help_text=(
            "The hostname of the machine"))

    domain = forms.CharField(
        label="Domain name", required=False, help_text=(
            "The domain name of the machine."))

    swap_size = forms.CharField(
        label="Swap size", required=False, help_text=(
            "The size of the swap file in bytes. The field also accepts K, M, "
            "G and T meaning kilobytes, megabytes, gigabytes and terabytes."))

    disable_ipv4 = forms.BooleanField(
        required=False, widget=forms.HiddenInput())

    class Meta:
        model = Node

        # Fields that the form should generate automatically from the
        # model:
        # Note: fields have to be added here even if they were defined manually
        # elsewhere in the form
        fields = (
            'hostname',
            'domain',
            'swap_size',
            )


class MachineForm(NodeForm):
    def __init__(self, request=None, *args, **kwargs):
        super(MachineForm, self).__init__(*args, **kwargs)

        # Even though it doesn't need it and doesn't use it, this form accepts
        # a parameter named 'request' because it is used interchangingly
        # with AdminMachineForm which actually uses this parameter.
        instance = kwargs.get('instance')

        self.set_up_architecture_field()
        # We only want the license key field to render in the UI if the `OS`
        # and `Release` fields are also present.
        if self.has_owner:
            self.set_up_osystem_and_distro_series_fields(instance)
            self.fields['license_key'] = forms.CharField(
                label="License Key", required=False, help_text=(
                    "License key for operating system"),
                max_length=30)
        else:
            self.fields['license_key'] = forms.CharField(
                label="", required=False, widget=forms.HiddenInput())

    def set_up_architecture_field(self):
        """Create the `architecture` field.

        This needs to be done on the fly so that we can pass a dynamic list of
        usable architectures.
        """
        architectures = list_all_usable_architectures()
        default_arch = pick_default_architecture(architectures)
        if len(architectures) == 0:
            choices = [BLANK_CHOICE]
        else:
            choices = list_architecture_choices(architectures)
        invalid_arch_message = compose_invalid_choice_text(
            'architecture', choices)
        self.fields['architecture'] = forms.ChoiceField(
            choices=choices, required=False, initial=default_arch,
            error_messages={'invalid_choice': invalid_arch_message})

    def set_up_osystem_and_distro_series_fields(self, instance):
        """Create the `osystem` and `distro_series` fields.

        This needs to be done on the fly so that we can pass a dynamic list of
        usable operating systems and distro_series.
        """
        osystems = list_all_usable_osystems()
        releases = list_all_usable_releases(osystems)
        if self.has_owner:
            os_choices = list_osystem_choices(osystems)
            distro_choices = list_release_choices(releases)
            invalid_osystem_message = compose_invalid_choice_text(
                'osystem', os_choices)
            invalid_distro_series_message = compose_invalid_choice_text(
                'distro_series', distro_choices)
            self.fields['osystem'] = forms.ChoiceField(
                label="OS", choices=os_choices, required=False, initial='',
                error_messages={'invalid_choice': invalid_osystem_message})
            self.fields['distro_series'] = forms.ChoiceField(
                label="Release", choices=distro_choices,
                required=False, initial='',
                error_messages={
                    'invalid_choice': invalid_distro_series_message})
        else:
            self.fields['osystem'] = forms.ChoiceField(
                label="", required=False, widget=forms.HiddenInput())
            self.fields['distro_series'] = forms.ChoiceField(
                label="", required=False, widget=forms.HiddenInput())
        if instance is not None:
            initial_value = get_distro_series_initial(osystems, instance)
            if instance is not None:
                self.initial['distro_series'] = initial_value

    def clean_distro_series(self):
        return clean_distro_series_field(self, 'distro_series', 'osystem')

    def clean_min_hwe_kernel(self):
        min_hwe_kernel = self.cleaned_data.get('min_hwe_kernel')
        if self.new_node and not min_hwe_kernel:
            min_hwe_kernel = Config.objects.get_config(
                'default_min_hwe_kernel')
        return validate_min_hwe_kernel(min_hwe_kernel)

    def clean(self):
        cleaned_data = super(MachineForm, self).clean()

        if not self.instance.hwe_kernel:
            osystem = cleaned_data.get('osystem')
            distro_series = cleaned_data.get('distro_series')
            architecture = cleaned_data.get('architecture')
            min_hwe_kernel = cleaned_data.get('min_hwe_kernel')
            hwe_kernel = cleaned_data.get('hwe_kernel')
            try:
                cleaned_data['hwe_kernel'] = validate_hwe_kernel(
                    hwe_kernel, min_hwe_kernel, architecture, osystem,
                    distro_series)
            except ValidationError as e:
                set_form_error(self, 'hwe_kernel', e.message)
        return cleaned_data

    def is_valid(self):
        is_valid = super(MachineForm, self).is_valid()
        if not is_valid:
            return False
        if len(list_all_usable_architectures()) == 0:
            set_form_error(
                self, "architecture", NO_ARCHITECTURES_AVAILABLE)
            is_valid = False
        return is_valid

    def clean_license_key(self):
        """Validates the license_key field is the correct format for the
        selected operating system."""
        # We allow the license_key field to be blank, even if the OS requires
        # a license key. This is to allow for situations where the OS has a
        # license key installed in the image that gets deployed, or where the
        # OS is activated using some other activation service (for example
        # Windows KMS activation).
        key = self.cleaned_data.get('license_key')
        if key == '':
            return ''

        os_name = self.cleaned_data.get('osystem')
        series = self.cleaned_data.get('distro_series')
        if os_name == '':
            return ''

        if not validate_license_key(os_name, series, key):
            raise ValidationError("Invalid license key.")
        return key

    def set_distro_series(self, series=''):
        """Sets the osystem and distro_series, from the provided
        distro_series.
        """
        # This implementation is used so that current API, is not broken. This
        # makes the distro_series a flat namespace. The distro_series is used
        # to search through the supporting operating systems, to find the
        # correct operating system that supports this distro_series.
        self.is_bound = True
        self.data['osystem'] = ''
        self.data['distro_series'] = ''
        if series is not None and series != '':
            osystem, release = find_osystem_and_release_from_release_name(
                series)
            if osystem is not None:
                key_required = get_release_requires_key(release)
                self.data['osystem'] = osystem['name']
                self.data['distro_series'] = '%s/%s%s' % (
                    osystem['name'],
                    release['name'],
                    key_required,
                    )
            else:
                self.data['distro_series'] = series

    def set_license_key(self, license_key=''):
        """Sets the license key."""
        self.is_bound = True
        self.data['license_key'] = license_key

    def set_hwe_kernel(self, hwe_kernel=''):
        """Sets the hwe_kernel."""
        self.is_bound = True
        self.data['hwe_kernel'] = hwe_kernel

    def set_install_rackd(self, install_rackd=False):
        """Sets whether to deploy the rack alongside this machine."""
        self.is_bound = True
        self.data['install_rackd'] = install_rackd

    class Meta:
        model = Machine

        fields = NodeForm.Meta.fields + (
            'architecture',
            'osystem',
            'distro_series',
            'license_key',
            'min_hwe_kernel',
            'hwe_kernel',
            'install_rackd',
        )


class DeviceForm(NodeForm):
    parent = forms.ModelChoiceField(
        required=False, initial=None,
        queryset=Node.objects.all(), to_field_name='system_id')

    zone = forms.ModelChoiceField(
        label="Physical zone", required=False,
        initial=Zone.objects.get_default_zone,
        queryset=Zone.objects.all(), to_field_name='name')

    class Meta:
        model = Device

        fields = NodeForm.Meta.fields + (
            'parent',
            'zone',
        )

    def __init__(self, request=None, *args, **kwargs):
        super(DeviceForm, self).__init__(*args, **kwargs)
        self.request = request

        instance = kwargs.get('instance')
        self.set_up_initial_device(instance)
        if instance is not None:
            self.initial['zone'] = instance.zone.name

    def set_up_initial_device(self, instance):
        """Initialize the 'parent' field if a device instance was given.

        This is a workaround for Django bug #17657.
        """
        if instance is not None and instance.parent is not None:
            self.initial['parent'] = instance.parent.system_id

    def save(self, commit=True):
        device = super(DeviceForm, self).save(commit=False)
        device.node_type = NODE_TYPE.DEVICE
        if self.new_node:
            # Set the owner: devices are owned by their creator.
            device.owner = self.request.user

        # If the device has a parent and no domain was provided,
        # inherit the parent's domain.
        if device.parent:
            if (not self.cleaned_data.get('domain', None) and
                    device.parent.domain):
                device.domain = device.parent.domain

        zone = self.cleaned_data.get('zone')
        if zone:
            device.zone = zone
        device.save()
        return device


class ControllerForm(MAASModelForm, WithPowerTypeMixin):

    class Meta:
        model = Controller

        fields = ['zone', 'domain']

    zone = forms.ModelChoiceField(
        label="Physical zone", required=False,
        initial=Zone.objects.get_default_zone,
        queryset=Zone.objects.all(), to_field_name='name')

    domain = forms.ModelChoiceField(
        required=False, initial=Domain.objects.get_default_domain,
        queryset=Domain.objects.all(), to_field_name='name')

    def __init__(self, data=None, instance=None, request=None, **kwargs):
        super(ControllerForm, self).__init__(
            data=data, instance=instance, **kwargs)
        WithPowerTypeMixin.set_up_power_fields(self, data, instance)
        if instance is not None:
            self.initial['zone'] = instance.zone.name
            self.initial['domain'] = instance.domain.name

    def clean(self):
        cleaned_data = super(ControllerForm, self).clean()
        return WithPowerTypeMixin.check_driver(self, cleaned_data)

    def save(self, *args, **kwargs):
        """Persist the node into the database."""
        controller = super(ControllerForm, self).save(commit=False)
        zone = self.cleaned_data.get('zone')
        if zone:
            controller.zone = zone
        WithPowerTypeMixin.set_values(self, controller)
        controller.save()
        return controller


NO_ARCHITECTURES_AVAILABLE = mark_safe(
    "No architectures are available to use for this node; boot images may not "
    "have been imported on the selected rack controller, or it may be "
    "unavailable."
)


class AdminNodeForm(NodeForm):
    """A `NodeForm` which includes fields that only an admin may change."""
    zone = forms.ModelChoiceField(
        label="Physical zone", required=False,
        initial=Zone.objects.get_default_zone,
        queryset=Zone.objects.all(), to_field_name='name')
    pool = forms.ModelChoiceField(
        label="Resource pool", required=False,
        initial=ResourcePool.objects.get_default_resource_pool,
        queryset=ResourcePool.objects.all(), to_field_name='name')
    cpu_count = forms.IntegerField(
        required=False, initial=0, label="CPU Count")
    memory = forms.IntegerField(
        required=False, initial=0, label="Memory (MiB)")

    class Meta:
        model = Node

        # Fields that the form should generate automatically from the
        # model:
        fields = NodeForm.Meta.fields + (
            'cpu_count',
            'memory',
        )

    def __init__(self, data=None, instance=None, request=None, **kwargs):
        super(AdminNodeForm, self).__init__(
            data=data, instance=instance, **kwargs)
        self.request = request
        self.set_up_initial_zone(instance)
        # The zone field is not required because we want to be able
        # to omit it when using that form in the API.
        # We don't want the UI to show an entry for the 'empty' zone,
        # in the zones dropdown.  This is why we set 'empty_label' to
        # None to force Django not to display that empty entry.
        self.fields['zone'].empty_label = None

    def set_up_initial_zone(self, instance):
        """Initialise `zone` field if a node instance was given.

        This works around Django bug 17657: the zone field refers to a zone
        by name, not by ID, yet Django attempts to initialise it with an ID.
        That doesn't work, and so without this workaround the field would
        revert to the default zone.
        """
        if instance is not None:
            self.initial['zone'] = instance.zone.name

    def save(self, *args, **kwargs):
        """Persist the node into the database."""
        node = super(AdminNodeForm, self).save(commit=False)
        zone = self.cleaned_data.get('zone')
        if zone:
            node.zone = zone
        if kwargs.get('commit', True):
            node.save(*args, **kwargs)
            self.save_m2m()  # Save many to many relations.
        return node


class AdminMachineForm(MachineForm, AdminNodeForm, WithPowerTypeMixin):
    """A `MachineForm` which includes fields that only an admin may change."""

    class Meta:
        model = Machine

        # Fields that the form should generate automatically from the
        # model:
        fields = MachineForm.Meta.fields + (
            'cpu_count',
            'memory',
        )

    def __init__(self, data=None, instance=None, request=None, **kwargs):
        super(AdminMachineForm, self).__init__(
            data=data, instance=instance, **kwargs)
        WithPowerTypeMixin.set_up_power_fields(self, data, instance)

    def clean(self):
        cleaned_data = super(AdminMachineForm, self).clean()
        return WithPowerTypeMixin.check_driver(self, cleaned_data)

    def save(self, *args, **kwargs):
        """Persist the node into the database."""
        machine = super(AdminMachineForm, self).save(commit=False)
        zone = self.cleaned_data.get('zone')
        if zone:
            machine.zone = zone
        pool = self.cleaned_data.get('pool')
        if pool:
            machine.pool = pool
        WithPowerTypeMixin.set_values(self, machine)
        if kwargs.get('commit', True):
            machine.save(*args, **kwargs)
            self.save_m2m()  # Save many to many relations.
        return machine


def get_machine_edit_form(user):
    if user.is_superuser:
        return AdminMachineForm
    else:
        return MachineForm


def get_node_edit_form(user):
    if user.is_superuser:
        return AdminNodeForm
    else:
        return NodeForm


class KeyForm(MAASModelForm):
    """Base class for `SSHKeyForm` and `SSLKeyForm`."""

    def __init__(self, user, *args, **kwargs):
        super(KeyForm, self).__init__(*args, **kwargs)
        self.user = user

    def validate_unique(self):
        # This is a trick to work around a problem in Django.
        # See https://code.djangoproject.com/ticket/13091#comment:19 for
        # details.
        # Without this overridden validate_unique the validation error that
        # can occur if this user already has the same key registered would
        # occur when save() would be called.  The error would be an
        # IntegrityError raised when inserting the new key in the database
        # rather than a proper ValidationError raised by 'clean'.

        # Set the instance user.
        self.instance.user = self.user

        # Allow checking against the missing attribute.
        exclude = self._get_validation_exclusions()
        exclude.remove('user')
        try:
            self.instance.validate_unique(exclude=exclude)
        except ValidationError as e:
            # Publish this error as a 'key' error rather than a 'general'
            # error because only the 'key' errors are displayed on the
            # 'add key' form.
            error = e.message_dict.pop('__all__')
            self._errors.setdefault('key', self.error_class()).extend(error)


class SSHKeyForm(MAASModelForm):
    key = UnstrippedCharField(
        label="Public key",
        widget=forms.Textarea(attrs={'rows': '5', 'cols': '30'}),
        required=True)

    class Meta:
        model = SSHKey
        fields = ["key"]

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.user = user

    def save(self, endpoint, request):
        sshkey = super(SSHKeyForm, self).save()
        create_audit_event(
            EVENT_TYPES.AUTHORISATION, endpoint, request, None,
            description=("SSH key created by '%(username)s'."))
        return sshkey


class SSLKeyForm(KeyForm):
    key = UnstrippedCharField(
        label="SSL key",
        widget=forms.Textarea(attrs={'rows': '15', 'cols': '30'}),
        required=True)

    class Meta:
        model = SSLKey
        fields = ["key"]

    def save(self, endpoint, request):
        sslkey = super(SSLKeyForm, self).save()
        create_audit_event(
            EVENT_TYPES.AUTHORISATION, endpoint, request, None,
            description="SSL key created by '%(username)s'.")
        return sslkey


class MultipleMACAddressField(forms.MultiValueField):

    def __init__(self, nb_macs=1, *args, **kwargs):
        fields = [MACAddressFormField() for _ in range(nb_macs)]
        super(MultipleMACAddressField, self).__init__(fields, *args, **kwargs)

    def compress(self, data_list):
        if data_list:
            return data_list
        return []


IP_BASED_HOSTNAME_REGEXP = re.compile('\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}$')

MAX_MESSAGES = 10


def merge_error_messages(summary, errors, limit=MAX_MESSAGES):
    """Merge a collection of errors into a summary message of limited size.

    :param summary: The message summarizing the error.
    :type summary: unicode
    :param errors: The list of errors to merge.
    :type errors: iterable
    :param limit: The maximum number of individual error messages to include in
        the summary error message.
    :type limit: int
    """
    # Django might pass an django.forms.utils.ErrorList instance, which
    # pretends to be a list but then misbehaves. See ErrorList.__getitem__ for
    # an example. A work-around is to first get the messages out... which we
    # can do by listifying it.
    errors = list(errors)

    ellipsis_msg = ''
    if len(errors) > limit:
        nb_errors = len(errors) - limit
        ellipsis_msg = (
            " and %d more error%s" % (
                nb_errors,
                's' if nb_errors > 1 else ''))
    return "%s (%s%s)" % (
        summary,
        ' \u2014 '.join(errors[:limit]),
        ellipsis_msg
    )


class WithMACAddressesMixin:
    """A form mixin which dynamically adds a MultipleMACAddressField to the
    list of fields.  This mixin also overrides the 'save' method to persist
    the list of MAC addresses and is intended to be used with a class
    inheriting from NodeForm.
    """

    def __init__(self, *args, **kwargs):
        super(WithMACAddressesMixin, self).__init__(*args, **kwargs)
        # This is a workaround for a Django bug in which self.data (which is
        # supposed to be a QueryDict) ends up being a normal Python dict.
        # This class requires a QueryDict (which it seems like Django should
        # enforce, for consistency).
        if not isinstance(self.data, QueryDict):
            self.data = get_QueryDict(self.data)
        self.set_up_mac_addresses_field()

    def set_up_mac_addresses_field(self):
        macs = [mac for mac in self.data.getlist('mac_addresses') if mac]
        self.fields['mac_addresses'] = MultipleMACAddressField(len(macs))
        self.data = self.data.copy()
        self.data['mac_addresses'] = macs

    def is_valid(self):
        valid = super(WithMACAddressesMixin, self).is_valid()
        # If the number of MAC address fields is > 1, provide a unified
        # error message if the validation has failed.
        reformat_mac_address_error = (
            self.errors.get('mac_addresses', None) is not None and
            len(self.data['mac_addresses']) > 1)
        if reformat_mac_address_error:
            self.errors['mac_addresses'] = [merge_error_messages(
                "One or more MAC addresses is invalid.",
                self.errors['mac_addresses'])]
        return valid

    def _mac_in_use_on_node_error(self, mac, node):
        """Returns an error string to be used wihen the specified MAC
        is already in use on the specified Node model object."""
        return "MAC address %s already in use%s." % (
            mac, " on %s" % node.hostname if node else '')

    def clean_mac_addresses(self):
        data = self.cleaned_data['mac_addresses']
        errors = []
        for mac in data:
            if self.instance.id is not None:
                query = Interface.objects.filter(
                    mac_address=mac.lower()).exclude(
                    node=self.instance).exclude(type=INTERFACE_TYPE.UNKNOWN)
            else:
                # This node does not exist yet, we should only check if this
                # MAC address is already attached to another node.
                query = Interface.objects.filter(
                    mac_address=mac.lower()).exclude(
                    type=INTERFACE_TYPE.UNKNOWN)
            for iface in query:
                node = iface.node
                errors.append(self._mac_in_use_on_node_error(mac, node))
        if errors:
            raise ValidationError(errors)
        return data

    def save(self):
        """Save the form's data to the database.

        This implementation of `save` does not support the `commit` argument.
        """
        node = super(WithMACAddressesMixin, self).save()
        for mac in self.cleaned_data['mac_addresses']:
            mac_addresses_errors = []
            try:
                node.add_physical_interface(mac)
            except ValidationError as e:
                mac_addresses_errors.append(e.message)
            if mac_addresses_errors:
                raise ValidationError({
                    "mac_addresses": mac_addresses_errors
                    })
        # Generate a hostname for this node if the provided hostname is
        # IP-based (because this means that this name comes from a DNS
        # reverse query to the MAAS DNS).  If the provided hostname was empty,
        # that was randomized in Node.save().
        if IP_BASED_HOSTNAME_REGEXP.match(node.hostname) is not None:
            node.set_random_hostname()
        return node


class AdminMachineWithMACAddressesForm(
        WithMACAddressesMixin, AdminMachineForm):
    """A version of the AdminMachineForm which includes the multi-MAC address
    field.
    """


class MachineWithMACAddressesForm(WithMACAddressesMixin, MachineForm):
    """A version of the MachineForm which includes the multi-MAC address field.
    """


class MachineWithPowerAndMACAddressesForm(
        WithPowerTypeMixin, MachineWithMACAddressesForm):
    """A version of the MachineForm which includes the power fields.
    """


class DeviceWithMACsForm(WithMACAddressesMixin, DeviceForm):
    """A version of the DeviceForm which includes the multi-MAC address field.
    """


def get_machine_create_form(user):
    if user.is_superuser:
        return AdminMachineWithMACAddressesForm
    else:
        return MachineWithPowerAndMACAddressesForm


class ProfileForm(MAASModelForm):
    # We use the field 'last_name' to store the user's full name (and
    # don't display Django's 'first_name' field).
    last_name = forms.CharField(
        label="Full name", max_length=30, required=False)

    # We use the email field for Ubuntu SSO.
    email = forms.EmailField(
        label="Email address (SSO)", required=False, help_text=(
            "Ubuntu Core deployments will use this email address to register "
            "the system with Ubuntu SSO."))

    class Meta:
        model = User
        fields = ('last_name', 'email')


class NewUserCreationForm(UserCreationForm):
    is_superuser = forms.BooleanField(
        label="MAAS administrator", required=False)
    last_name = forms.CharField(
        label="Full name", max_length=30, required=False)
    email = forms.EmailField(
        label="E-mail address", max_length=75, required=True)

    class Meta(UserCreationForm.Meta):
        fields = (
            'username',
            'last_name',
            'email',
            'password1',
            'password2',
            'is_superuser',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.external_auth_enabled = (
            Config.objects.is_external_auth_enabled())
        if self.external_auth_enabled:
            del self.fields['password1']
            del self.fields['password2']

    def clean(self):
        super().clean()
        if self.external_auth_enabled:
            # add back data for password fields, as save() needs them
            self.cleaned_data['password1'] = None
            self.cleaned_data['password2'] = None

    def save(self, commit=True):
        user = super(NewUserCreationForm, self).save(commit=False)
        if self.cleaned_data.get('is_superuser', False):
            user.is_superuser = True
        new_last_name = self.cleaned_data.get('last_name', None)
        if new_last_name is not None:
            user.last_name = new_last_name
        new_email = self.cleaned_data.get('email', None)
        if new_email is not None:
            user.email = new_email
        if commit:
            user.save()
        return user

    def clean_email(self):
        """Validate that the supplied email address is unique for the
        site.
        """
        email = self.cleaned_data['email']
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "User with this E-mail address already exists.")
        return email


class EditUserForm(UserChangeForm):
    # Override the default label.
    is_superuser = forms.BooleanField(
        label="MAAS administrator", required=False)
    last_name = forms.CharField(
        label="Full name", max_length=30, required=False)

    class Meta:
        model = User
        fields = (
            'username', 'last_name', 'email', 'is_superuser')

    def __init__(self, *args, **kwargs):
        super(EditUserForm, self).__init__(*args, **kwargs)
        # Django 1.4 overrides the field 'password' thus adding it
        # post-facto to the list of the selected fields (Meta.fields).
        # Here we don't want to use this form to edit the password.
        if 'password' in self.fields:
            del self.fields['password']


class DeleteUserForm(Form):
    """A form to remove a user."""

    transfer_resources_to = forms.CharField(
        label="Transfer resource to", required=False,
        help_text="Transfer resources owned by the user to this user.")


class ConfigForm(Form):
    """A base class for forms that save the content of their fields into
    Config objects.
    """
    # List of fields that should be considered configuration fields.
    # Consider all the fields as configuration fields if this is None.
    config_fields = None

    def __init__(self, *args, **kwargs):
        super(ConfigForm, self).__init__(*args, **kwargs)
        if 'initial' not in kwargs:
            self._load_initials()

    def _load_initials(self):
        self.initial = {}
        for name in self.fields:
            conf = Config.objects.get_config(name)
            if conf is not None:
                self.initial[name] = conf

    def clean(self):
        cleaned_data = super(Form, self).clean()
        for config_name in cleaned_data:
            consider_field = (
                self.config_fields is None or
                config_name in self.config_fields
            )
            if consider_field:
                if config_name not in CONFIG_ITEMS_KEYS:
                    self._errors[config_name] = self.error_class([
                        INVALID_SETTING_MSG_TEMPLATE % config_name])
        return cleaned_data

    def save(self, endpoint, request):
        """Save the content of the fields into the database.

        This implementation of `save` does not support the `commit` argument.

        :return: Whether or not the content of the fields was valid and hence
            sucessfully saved into the database.
        :rtype: boolean
        """
        self.full_clean()
        if self._errors:
            return False
        else:
            for name, value in self.cleaned_data.items():
                consider_field = (
                    self.config_fields is None or
                    name in self.config_fields
                )
                if consider_field:
                    Config.objects.set_config(name, value, endpoint, request)
            return True


class MAASForm(ConfigForm):
    """Settings page, MAAS section."""
    maas_name = get_config_field('maas_name')
    enable_analytics = get_config_field('enable_analytics')


class ProxyForm(ConfigForm):
    """Settings page, Proxy section."""
    enable_http_proxy = get_config_field('enable_http_proxy')
    use_peer_proxy = get_config_field('use_peer_proxy')
    http_proxy = get_config_field('http_proxy')
    # LP: #1787381 - Fix an issue where the UI is overriding config fields
    # that are *only* exposed over the API.
    #
    # XXX - since the UI for these options has been converted to Angular,
    # MAAS no longer automatically creates fields for these based on the
    # settings forms. As such, this form doesn't validate against the
    # settings form (as the DNSForm would do, for example). As such
    # .
    # These fields need to be added back once LP: #1787467 is fixed.
    # prefer_v4_proxy = get_config_field('prefer_v4_proxy')
    # maas_proxy_port = get_config_field('maas_proxy_port')


class DNSForm(ConfigForm):
    """Settings page, DNS section."""
    upstream_dns = get_config_field('upstream_dns')
    dnssec_validation = get_config_field('dnssec_validation')


class NTPForm(ConfigForm):
    """Settings page, NTP section."""
    ntp_servers = get_config_field('ntp_servers')
    ntp_external_only = get_config_field('ntp_external_only')


class NetworkDiscoveryForm(ConfigForm):
    """Settings page, Network Discovery section."""
    network_discovery = get_config_field('network_discovery')
    active_discovery_interval = get_config_field('active_discovery_interval')


class ThirdPartyDriversForm(ConfigForm):
    """Settings page, Third Party Drivers section."""
    enable_third_party_drivers = get_config_field('enable_third_party_drivers')


class StorageSettingsForm(ConfigForm):
    """Settings page, storage section."""
    default_storage_layout = get_config_field('default_storage_layout')
    enable_disk_erasing_on_release = get_config_field(
        'enable_disk_erasing_on_release')
    disk_erase_with_secure_erase = get_config_field(
        'disk_erase_with_secure_erase')
    disk_erase_with_quick_erase = get_config_field(
        'disk_erase_with_quick_erase')


class CommissioningForm(ConfigForm):
    """Settings page, Commissioning section."""

    def __init__(self, *args, **kwargs):
        # Skip ConfigForm.__init__ because we need the form intialized but
        # don't want _load_initial called until the field has been added.
        Form.__init__(self, *args, **kwargs)
        self.fields['commissioning_distro_series'] = get_config_field(
            'commissioning_distro_series')
        self.fields['default_min_hwe_kernel'] = get_config_field(
            'default_min_hwe_kernel')
        self._load_initials()


class DeployForm(ConfigForm):
    """Settings page, Deploy section."""

    def __init__(self, *args, **kwargs):
        # Skip ConfigForm.__init__ because we need the form intialized but
        # don't want _load_initial called until the field has been added.
        Form.__init__(self, *args, **kwargs)
        self.fields['default_osystem'] = get_config_field('default_osystem')
        self.fields['default_distro_series'] = (
            self._get_default_distro_series_field_for_ui())
        self._load_initials()

    def _get_default_distro_series_field_for_ui(self):
        """This create the field with os/release. This is needed by the UI
        to filter the releases based on the OS selection. The API uses the
        field defined in settings.py"""
        usable_oses = list_all_usable_osystems()
        release_choices = list_release_choices(
            list_all_usable_releases(usable_oses), include_default=False)
        if len(release_choices) == 0:
            release_choices = [('---', '--- No Usable Release ---')]
        field = forms.ChoiceField(
            initial=Config.objects.get_config('default_distro_series'),
            choices=release_choices,
            validators=[validate_missing_boot_images],
            error_messages={
                'invalid_choice': compose_invalid_choice_text(
                    'release', release_choices)
            },
            label="Default OS release used for deployment",
            required=False)
        return field

    def _load_initials(self):
        super(DeployForm, self)._load_initials()
        initial_os = self.fields['default_osystem'].initial
        initial_series = self.fields['default_distro_series'].initial
        self.initial['default_distro_series'] = '%s/%s' % (
            initial_os,
            initial_series
            )

    def clean_default_distro_series(self):
        return clean_distro_series_field(
            self, 'default_distro_series', 'default_osystem')


class UbuntuForm(Form):
    """Used to access legacy package archives via the Legacy API."""
    main_archive = forms.URLField(
        label="Main archive", required=True,
        help_text=(
            "Archive used by nodes to retrieve packages for Intel "
            "architectures, e.g. http://archive.ubuntu.com/ubuntu."))
    ports_archive = forms.URLField(
        label="Ports archive", required=True,
        help_text=(
            "Archive used by nodes to retrieve packages for non-Intel "
            "architectures, e.g. http://ports.ubuntu.com/ubuntu-ports."))

    def __init__(self, *args, **kwargs):
        super(UbuntuForm, self).__init__(*args, **kwargs)
        self._load_initials()

    def _load_initials(self):
        """Load the initial values for the fields."""
        self.initial['main_archive'] = (
            PackageRepository.get_main_archive().url)
        self.initial['ports_archive'] = (
            PackageRepository.get_ports_archive().url)

    def save(self, *args, **kwargs):
        """Save the content of the fields into the database.

        This implementation of `save` does not support the `commit` argument.

        :return: Whether or not the content of the fields was valid and hence
            sucessfully saved into the database.
        :rtype: boolean
        """
        if self._errors:
            return False
        PackageRepository.objects.update_or_create(
            name='main_archive',
            defaults={
                'url': self.cleaned_data['main_archive'],
                'arches': PackageRepository.MAIN_ARCHES,
                'default': True,
                'enabled': True})
        PackageRepository.objects.update_or_create(
            name='ports_archive',
            defaults={
                'url': self.cleaned_data['ports_archive'],
                'arches': PackageRepository.PORTS_ARCHES,
                'default': True,
                'enabled': True})
        return True


class WindowsForm(ConfigForm):
    """Settings page, Windows section."""
    windows_kms_host = get_config_field('windows_kms_host')


class GlobalKernelOptsForm(ConfigForm):
    """Settings page, Global Kernel Parameters section."""
    kernel_opts = get_config_field('kernel_opts')


ERROR_MESSAGE_STATIC_IPS_OUTSIDE_RANGE = (
    "New static IP range does not include already-allocated IP "
    "addresses.")


ERROR_MESSAGE_STATIC_RANGE_IN_USE = (
    "Cannot remove static IP range when there are allocated IP addresses "
    "in that range.")


ERROR_MESSAGE_DYNAMIC_RANGE_SPANS_SLASH_16S = (
    "All addresses in the dynamic range must be within the same /16 "
    "network.")

ERROR_MESSAGE_INVALID_RANGE = (
    "Invalid IP range (high IP address must be higher than low IP address).")


def is_set(string):
    """Check that the string is actually set.

    :param string: string to test.
    :return: string is actually a non-empty string.
    """
    return (string is not None and len(string) > 0 and not string.isspace())


class TagForm(MAASModelForm):

    class Meta:
        model = Tag
        fields = (
            'name',
            'comment',
            'definition',
            'kernel_opts',
            )

    def clean_definition(self):
        definition = self.cleaned_data['definition']
        if not definition:
            return ""
        try:
            etree.XPath(definition)
        except etree.XPathSyntaxError as e:
            raise ValidationError('Invalid xpath expression: %s' % (e,))
        return definition


class UnconstrainedMultipleChoiceField(MultipleChoiceField):
    """A MultipleChoiceField which does not constrain the given choices."""

    def validate(self, value):
        return value


class ValidatorMultipleChoiceField(MultipleChoiceField):
    """A MultipleChoiceField validating each given choice with a validator."""

    def __init__(self, validator, **kwargs):
        super(ValidatorMultipleChoiceField, self).__init__(**kwargs)
        self.validator = validator

    def validate(self, values):
        for value in values:
            self.validator(value)
        return values


class InstanceListField(UnconstrainedMultipleChoiceField):
    """A multiple-choice field used to list model instances."""

    def __init__(self, model_class, field_name,
                 text_for_invalid_object=None,
                 *args, **kwargs):
        """Instantiate an InstanceListField.

        Build an InstanceListField to deal with a list of instances of
        the class `model_class`, identified their field named
        `field_name`.

        :param model_class:  The model class of the instances to list.
        :param field_name:  The name of the field used to retrieve the
            instances. This must be a unique field of the `model_class`.
        :param text_for_invalid_object:  Option error message used to
            create the validation error returned when any of the input
            values doesn't match an existing object.  The default value
            is "Unknown {obj_name}(s): {unknown_names}.".  A custom
            message can use {obj_name} and {unknown_names} which will be
            replaced by the name of the model instance and the list of
            the names that didn't correspond to a valid instance
            respectively.
        """
        super(InstanceListField, self).__init__(*args, **kwargs)
        self.model_class = model_class
        self.field_name = field_name
        if text_for_invalid_object is None:
            text_for_invalid_object = (
                "Unknown {obj_name}(s): {unknown_names}.")
        self.text_for_invalid_object = text_for_invalid_object

    def clean(self, value):
        """Clean the list of field values.

        Assert that each field value corresponds to an instance of the class
        `self.model_class`.
        """
        if value is None:
            return None
        # `value` is in fact a list of values since this field is a subclass of
        # forms.MultipleChoiceField.
        set_values = set(value)
        filters = {'%s__in' % self.field_name: set_values}

        instances = self.model_class.objects.filter(**filters)
        if len(instances) != len(set_values):
            unknown = set_values.difference(
                {getattr(instance, self.field_name) for instance in instances})
            error = self.text_for_invalid_object.format(
                obj_name=self.model_class.__name__.lower(),
                unknown_names=', '.join(sorted(unknown))
                )
            raise forms.ValidationError(error)
        return instances


class SetZoneBulkAction:
    """A custom action we only offer in bulk: "Set physical zone."

    Looks just enough like a node action class for presentation purposes, but
    isn't one of the actions we normally offer on the node page.  The
    difference is that this action takes an argument: the zone.
    """
    name = 'set_zone'
    display = "Set physical zone"


class BulkNodeActionForm(Form):
    # system_id is a multiple-choice field so it can actually contain
    # a list of system ids.
    system_id = UnconstrainedMultipleChoiceField()

    def __init__(self, user, *args, **kwargs):
        super(BulkNodeActionForm, self).__init__(*args, **kwargs)
        self.user = user
        action_choices = (
            # Put an empty action as the first displayed option to avoid
            # fat-fingered bulk actions.
            [('', 'Select Action')] +
            [(action.name, action.display) for action in ACTION_CLASSES]
            )
        add_zone_field = (
            user.is_superuser and
            (
                self.data == {} or
                self.data.get('action') == SetZoneBulkAction.name
            )
        )
        # Only admin users get the "set zone" bulk action.
        # The 'zone' field is required only if the form is being submitted
        # with the 'action' set to SetZoneBulkAction.name or when the UI is
        # rendering a GET request (i.e. the zone cannot be the empty string).
        # Thus it cannot be added to the form when the form is being
        # submitted with an action other than SetZoneBulkAction.name.
        if add_zone_field:
            action_choices.append(
                (SetZoneBulkAction.name, SetZoneBulkAction.display))
            # This adds an input field: the zone.
            self.fields['zone'] = forms.ModelChoiceField(
                label="Physical zone", required=True,
                initial=Zone.objects.get_default_zone(),
                queryset=Zone.objects.all(), to_field_name='name')
        self.fields['action'] = forms.ChoiceField(
            required=True, choices=action_choices)

    def clean_system_id(self):
        system_ids = self.cleaned_data['system_id']
        # Remove duplicates.
        system_ids = set(system_ids)
        if len(system_ids) == 0:
            raise forms.ValidationError("No node selected.")
        # Validate all the system ids.
        real_node_count = Node.objects.filter(
            system_id__in=system_ids).count()
        if real_node_count != len(system_ids):
            raise forms.ValidationError(
                "Some of the given system ids are invalid system ids.")
        return system_ids

    @transactional
    def _perform_action_on_node(self, system_id, action_class):
        """Perform a node action on the identified node.

        This is *transactional*, meaning it will commit its changes on
        success, and roll-back if not.

        Returns a string describing what was done, one of:

        * not_actionable
        * not_permitted
        * done

        :param system_id: A `Node.system_id` value.
        :param action_class: A value from `ACTIONS_DICT`.
        """
        node = Node.objects.get(system_id=system_id)
        if node.status in action_class.actionable_statuses:
            action_instance = action_class(node=node, user=self.user)
            if action_instance.inhibit() is not None:
                return "not_actionable"
            else:
                if action_instance.is_permitted():
                    # Do not let execute() raise a redirect exception
                    # because this action is part of a bulk operation.
                    try:
                        action_instance.execute()
                    except NodeActionError:
                        return "not_actionable"
                    else:
                        return "done"
                else:
                    return "not_permitted"
        else:
            return "not_actionable"

    @asynchronous(timeout=FOREVER)
    def _perform_action_on_nodes(
            self, system_ids, action_class, concurrency=2):
        """Perform a node action on the identified nodes.

        This is *asynchronous*.

        :param system_ids: An iterable of `Node.system_id` values.
        :param action_class: A value from `ACTIONS_DICT`.
        :param concurrency: The number of actions to run concurrently.

        :return: A `dict` mapping `system_id` to results, where the result can
            be a string (see `_perform_action_on_node`), or a `Failure` if
            something went wrong.
        """
        # We're going to be making the same call for every specified node, so
        # bundle up the common bits here to keep the noise down later on.
        perform = partial(
            deferToDatabase, self._perform_action_on_node,
            action_class=action_class)

        # The results will be a `system_id` -> `result` mapping, where
        # `result` can be a string like "done" or "not_actionable", or a
        # Failure instance.
        results = {}

        # Convenient callback.
        def record(result, system_id):
            results[system_id] = result

        # A *lazy* list of tasks to be run. It's very important that each task
        # is only created at the moment it's needed. Each task records its
        # outcome via `record`, be that success or failure.
        tasks = (
            perform(system_id).addBoth(record, system_id)
            for system_id in system_ids
        )

        # Create `concurrency` co-iterators. Each draws work from `tasks`.
        deferreds = (coiterate(tasks) for _ in range(concurrency))
        # Capture the moment when all the co-iterators have finished.
        done = DeferredList(deferreds, consumeErrors=True)
        # Return only the `results` mapping; ignore the result from `done`.

        return done.addCallback(lambda _: results)

    def perform_action(self, action_name, system_ids):
        """Perform a node action on the identified nodes.

        :param action_name: Name of a node action in `ACTIONS_DICT`.
        :param system_ids: Iterable of `Node.system_id` values.
        :return: A tuple as returned by `save`.
        """
        action_class = ACTIONS_DICT.get(action_name)
        results = self._perform_action_on_nodes(system_ids, action_class)
        # There is a lot of valuable information in `results`, including
        # failures, but currently we're only interested in basic stats.
        stats = Counter(
            result for result in results.values()
            if not isinstance(result, Failure))
        return stats["done"], stats["not_actionable"], stats["not_permitted"]

    def set_zone(self, system_ids):
        """Custom bulk action: set zone on identified nodes.

        :return: A tuple as returned by `save`.
        """
        zone = self.cleaned_data['zone']
        Node.objects.filter(system_id__in=system_ids).update(zone=zone)
        return (len(system_ids), 0, 0)

    def save(self, *args, **kwargs):
        """Perform the action on the selected nodes.

        This method returns a tuple containing 3 elements: the number of
        nodes for which the action was successfully performed, the number of
        nodes for which the action could not be performed because that
        transition was not allowed and the number of nodes for which the
        action could not be performed because the user does not have the
        required permission.

        Currently, in the event of a NodeActionError this is thrown into the
        "not actionable" bucket in lieu of an overhaul of this form to
        properly report errors for part-failing actions.  In this case
        the transaction will still be valid for the actions that did complete
        successfully.
        """
        action_name = self.cleaned_data['action']
        system_ids = self.cleaned_data['system_id']
        if action_name == SetZoneBulkAction.name:
            return self.set_zone(system_ids)
        else:
            return self.perform_action(action_name, system_ids)


class ZoneForm(MAASModelForm):

    class Meta:
        model = Zone
        fields = (
            'name',
            'description',
            )


class ResourcePoolForm(MAASModelForm):

    class Meta:
        model = ResourcePool
        fields = (
            'name',
            'description',
            )


class NodeMACAddressChoiceField(forms.ModelMultipleChoiceField):
    """A ModelMultipleChoiceField which shows the name of the MACs."""

    def label_from_instance(self, obj):
        return "%s (%s)" % (obj.mac_address, obj.node.hostname)


class NetworksListingForm(Form):
    """Form for the networks listing API."""

    # Multi-value parameter, but with a name in the singular.  This is going
    # to be passed as a GET-style parameter in the URL, so repeated as "node="
    # for every node.
    node = InstanceListField(
        model_class=Node, field_name='system_id',
        label="Show only networks that are attached to all of these nodes.",
        required=False, error_messages={
            'invalid_list':
            "Invalid parameter: list of node system IDs required.",
            })

    def filter_subnets(self, subnets):
        """Filter (and order) the given subnets by the form's criteria.

        :param subnets: A query set of :class:`Subnet`.
        :return: A version of `subnets` restricted and ordered according to
            the criteria passed to the form.
        """
        nodes = self.cleaned_data.get('node')
        if nodes is not None:
            for node in nodes:
                subnets = subnets.filter(staticipaddress__interface__node=node)
        return subnets.order_by('id')


class BootSourceForm(MAASModelForm):
    """Form for the Boot Source API."""

    class Meta:
        model = BootSource
        fields = (
            'url',
            'keyring_filename',
            'keyring_data',
            )

    keyring_filename = forms.CharField(
        label="The path to the keyring file for this BootSource.",
        required=False)

    keyring_data = forms.FileField(
        label="The GPG keyring for this BootSource, as a binary blob.",
        required=False)

    def __init__(self, **kwargs):
        super(BootSourceForm, self).__init__(**kwargs)

    def clean_keyring_data(self):
        """Process 'keyring_data' field.

        Return the InMemoryUploadedFile's content so that it can be
        stored in the boot source's 'keyring_data' binary field.
        """
        data = self.cleaned_data.get('keyring_data', None)
        if data is not None:
            return data.read()
        return data


class BootSourceSelectionForm(MAASModelForm):
    """Form for the Boot Source Selection API."""

    class Meta:
        model = BootSourceSelection
        fields = (
            'os',
            'release',
            'arches',
            'subarches',
            'labels',
            )

    # Use UnconstrainedMultipleChoiceField fields for multiple-choices
    # fields instead of the default (djorm-ext-pgarray's ArrayFormField):
    # ArrayFormField deals with comma-separated lists and here we want to
    # handle multiple-values submissions.
    arches = UnconstrainedMultipleChoiceField(label="Architecture list")
    subarches = UnconstrainedMultipleChoiceField(label="Subarchitecture list")
    labels = UnconstrainedMultipleChoiceField(label="Label list")

    def __init__(self, boot_source=None, **kwargs):
        super(BootSourceSelectionForm, self).__init__(**kwargs)
        if 'instance' in kwargs:
            self.boot_source = kwargs['instance'].boot_source
        else:
            self.boot_source = boot_source

    def clean(self):
        cleaned_data = super(BootSourceSelectionForm, self).clean()

        # Don't filter on OS if not provided. This is to maintain
        # backwards compatibility for when OS didn't exist in the API.
        if cleaned_data['os']:
            cache = BootSourceCache.objects.filter(
                boot_source=self.boot_source, os=cleaned_data['os'],
                release=cleaned_data['release'])
        else:
            cache = BootSourceCache.objects.filter(
                boot_source=self.boot_source, release=cleaned_data['release'])

        if not cache.exists():
            set_form_error(
                self, "os",
                "OS %s with release %s has no available images for download" %
                (cleaned_data['os'], cleaned_data['release']))
            return cleaned_data

        values = cache.values_list("arch", "subarch", "label")
        arches, subarches, labels = zip(*values)

        # Validate architectures.
        required_arches_set = set(arch for arch in cleaned_data['arches'])
        wildcard_arches = '*' in required_arches_set
        if not wildcard_arches and not required_arches_set <= set(arches):
            set_form_error(
                self, "arches",
                "No available images to download for %s" %
                cleaned_data['arches'])

        # Validate subarchitectures.
        required_subarches_set = set(sa for sa in cleaned_data['subarches'])
        wildcard_subarches = '*' in required_subarches_set
        if (
            not wildcard_subarches and
            not required_subarches_set <= set(subarches)
                ):
            set_form_error(
                self, "subarches",
                "No available images to download for %s" %
                cleaned_data['subarches'])

        # Validate labels.
        required_labels_set = set(label for label in cleaned_data['labels'])
        wildcard_labels = '*' in required_labels_set
        if not wildcard_labels and not required_labels_set <= set(labels):
            set_form_error(
                self, "labels",
                "No available images to download for %s" %
                cleaned_data['labels'])

        return cleaned_data

    def save(self, *args, **kwargs):
        boot_source_selection = super(
            BootSourceSelectionForm, self).save(commit=False)
        boot_source_selection.boot_source = self.boot_source
        if kwargs.get('commit', True):
            boot_source_selection.save()
        return boot_source_selection


class LicenseKeyForm(MAASModelForm):
    """Form for global license keys."""

    class Meta:
        model = LicenseKey
        fields = (
            'osystem',
            'distro_series',
            'license_key',
            )

    def __init__(self, *args, **kwargs):
        super(LicenseKeyForm, self).__init__(*args, **kwargs)
        self.set_up_osystem_and_distro_series_fields(kwargs.get('instance'))

    def set_up_osystem_and_distro_series_fields(self, instance):
        """Create the `osystem` and `distro_series` fields.

        This needs to be done on the fly so that we can pass a dynamic list of
        usable operating systems and distro_series.
        """
        osystems = list_all_usable_osystems()
        releases = list_all_releases_requiring_keys(osystems)

        # Remove the operating systems that do not have any releases that
        # require license keys. Don't want them to show up in the UI or be
        # used in the API.
        osystems = [
            osystem
            for osystem in osystems
            if osystem['name'] in releases
            ]

        os_choices = list_osystem_choices(osystems, include_default=False)
        distro_choices = list_release_choices(
            releases, include_default=False, with_key_required=False)
        invalid_osystem_message = compose_invalid_choice_text(
            'osystem', os_choices)
        invalid_distro_series_message = compose_invalid_choice_text(
            'distro_series', distro_choices)
        self.fields['osystem'] = forms.ChoiceField(
            label="OS", choices=os_choices, required=True,
            error_messages={'invalid_choice': invalid_osystem_message})
        self.fields['distro_series'] = forms.ChoiceField(
            label="Release", choices=distro_choices, required=True,
            error_messages={
                'invalid_choice': invalid_distro_series_message})
        if instance is not None:
            initial_value = get_distro_series_initial(
                osystems, instance, with_key_required=False)
            if instance is not None:
                self.initial['distro_series'] = initial_value

    def clean(self):
        """Validate distro_series and osystem match, and license_key is valid
        for selected operating system and series."""
        # Get the clean_data, check that all of the fields we need are
        # present. If not then the form will error, so no reason to continue.
        cleaned_data = super(LicenseKeyForm, self).clean()
        required_fields = ['license_key', 'osystem', 'distro_series']
        for field in required_fields:
            if field not in cleaned_data:
                return cleaned_data
        cleaned_data['distro_series'] = self.clean_osystem_distro_series_field(
            cleaned_data)
        self.validate_license_key(cleaned_data)
        return cleaned_data

    def clean_osystem_distro_series_field(self, cleaned_data):
        """Validate that os/distro_series matches osystem, and update the
        distro_series field, to remove the leading os/."""
        cleaned_osystem = cleaned_data['osystem']
        cleaned_series = cleaned_data['distro_series']
        series_os, release = cleaned_series.split('/', 1)
        if series_os != cleaned_osystem:
            raise ValidationError(
                "%s in distro_series does not match with "
                "operating system %s" % (release, cleaned_osystem))
        return release

    def validate_license_key(self, cleaned_data):
        """Validates that the license key is valid."""
        cleaned_key = cleaned_data['license_key']
        cleaned_osystem = cleaned_data['osystem']
        cleaned_series = cleaned_data['distro_series']
        if not validate_license_key(
                cleaned_osystem, cleaned_series, cleaned_key):
            raise ValidationError("Invalid license key.")


BOOT_RESOURCE_FILE_TYPE_CHOICES_UPLOAD = (
    ('tgz', "Root Image (tar.gz)"),
    ('ddtgz', "Root Compressed DD (dd -> tar.gz)"),
    ('ddtbz', "Root Compressed DD (dd -> root-dd.tar.bz2)"),
    ('ddtxz', "Root Compressed DD (dd -> root-dd.tar.xz)"),
    ('ddtar', "Root Tarfile with DD (dd -> root-dd.tar)"),
    ('ddbz2', "Root Compressed DD (root-dd.bz2)"),
    ('ddgz', "Root Compressed DD (root-dd.gz)"),
    ('ddxz', "Root Compressed DD (root-dd.xz)"),
    ('ddraw', "Raw root DD image(dd -> root-dd.raw)"),
    )


class BootResourceForm(MAASModelForm):
    """Form for uploading boot resources."""

    class Meta:
        model = BootResource
        fields = (
            'name',
            'title',
            'architecture',
            'filetype',
            'content',
            )

    title = forms.CharField(label="Title", required=False)

    filetype = forms.ChoiceField(
        label="Filetype",
        choices=BOOT_RESOURCE_FILE_TYPE_CHOICES_UPLOAD,
        required=True, initial='tgz')

    content = forms.FileField(
        label="File", allow_empty_file=False)

    def __init__(self, *args, **kwargs):
        super(BootResourceForm, self).__init__(*args, **kwargs)
        self.set_up_architecture_field()

    def set_up_architecture_field(self):
        """Create the `architecture` field.

        This needs to be done on the fly so that we can pass a dynamic list of
        usable architectures.
        """
        architectures = list_all_usable_architectures()
        default_arch = pick_default_architecture(architectures)
        if len(architectures) == 0:
            choices = [BLANK_CHOICE]
        else:
            choices = list_architecture_choices(architectures)
        invalid_arch_message = compose_invalid_choice_text(
            'architecture', choices)
        self.fields['architecture'] = forms.ChoiceField(
            choices=choices, required=True, initial=default_arch,
            error_messages={'invalid_choice': invalid_arch_message})

    def clean_name(self):
        """Clean the name field.

        The 'custom/' is reserved for custom uploaded images and should not
        be present in the name field when uploaded. This allows users to
        provide 'custom/' where it will be removed and the image will be marked
        uploaded. Without this the image would be uploaded as Generated for
        a custom OS which is an invalid boot resource.
        """
        name = self.cleaned_data['name']
        if name.startswith('custom/'):
            name = name[7:]

        # Prevent the user from uploading any osystem/release or system name
        # already used in the SimpleStreams.
        reserved_names = [
            '%s/%s' % (bsc['os'], bsc['release'])
            for bsc in BootSourceCache.objects.values(
                'os', 'release').distinct()
        ]
        reserved_names += [
            i for name in reserved_names for i in name.split('/')] + [
            i['name'] for i in gen_all_known_operating_systems()]

        # Reserve CentOS version names for future MAAS use.
        if name in reserved_names or re.search('^centos\d\d?$', name):
            raise ValidationError('%s is a reserved name' % name)
        return name

    def get_existing_resource(self, resource):
        """Return existing resource if avaliable.

        If the passed resource already has a match in the database then that
        resource is returned. If not then the passed resource is returned.
        """
        if resource.rtype == BOOT_RESOURCE_TYPE.UPLOADED:
            # Uploaded BootResources were previously generated, now they're
            # uploaded. Search for both to convert.
            rtypes = [
                BOOT_RESOURCE_TYPE.UPLOADED, BOOT_RESOURCE_TYPE.GENERATED]
        else:
            rtypes = [resource.type]
        existing_resource = get_one(
            BootResource.objects.filter(
                rtype__in=rtypes,
                name=resource.name, architecture=resource.architecture))
        if existing_resource is not None:
            existing_resource.rtype = resource.rtype
            return existing_resource
        return resource

    def create_resource_set(self, resource, label):
        """Creates a new `BootResourceSet` on the given resource."""
        return BootResourceSet.objects.create(
            resource=resource,
            version=resource.get_next_version_name(), label=label)

    def get_resource_filetype(self, value):
        """Convert the upload filetype to the filetype for `BootResource`."""
        filetypes = {
            'tgz': BOOT_RESOURCE_FILE_TYPE.ROOT_TGZ,
            'ddtgz': BOOT_RESOURCE_FILE_TYPE.ROOT_DDTGZ,
            'ddtar': BOOT_RESOURCE_FILE_TYPE.ROOT_DDTAR,
            'ddraw': BOOT_RESOURCE_FILE_TYPE.ROOT_DDRAW,
            'ddtbz': BOOT_RESOURCE_FILE_TYPE.ROOT_DDTBZ,
            'ddtxz': BOOT_RESOURCE_FILE_TYPE.ROOT_DDTXZ,
            'ddbz2': BOOT_RESOURCE_FILE_TYPE.ROOT_DDBZ2,
            'ddgz': BOOT_RESOURCE_FILE_TYPE.ROOT_DDGZ,
            'ddxz': BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ
        }
        return filetypes.get(value)

    def create_resource_file(self, resource_set, data):
        """Creates a new `BootResourceFile` on the given resource set."""
        filetype = self.get_resource_filetype(data['filetype'])
        largefile = LargeFile.objects.get_or_create_file_from_content(
            data['content'])
        return BootResourceFile.objects.create(
            resource_set=resource_set, largefile=largefile,
            filename=filetype, filetype=filetype)

    def validate_unique(self):
        """Override to allow the same `BootResource` to already exist.

        This is done because the existing `BootResource` will be used, and a
        new set will be added to that resource.
        """
        # Do nothing, as we do not want to report a uniqueness error.

    def save(self):
        """Persist the boot resource into the database.

        This implementation of `save` does not support the `commit` argument.
        """
        resource = super(BootResourceForm, self).save(commit=False)
        resource.rtype = BOOT_RESOURCE_TYPE.UPLOADED
        resource = self.get_existing_resource(resource)
        resource.extra = {'subarches': resource.architecture.split('/')[1]}
        if 'title' in self.cleaned_data:
            resource.extra['title'] = self.cleaned_data['title']

        resource.save()
        resource_set = self.create_resource_set(resource, 'uploaded')
        self.create_resource_file(
            resource_set, self.cleaned_data)
        return resource


class BootResourceNoContentForm(BootResourceForm):
    """Form for uploading boot resources with no content."""

    class Meta:
        model = BootResource
        fields = (
            'name',
            'title',
            'architecture',
            'filetype',
            'sha256',
            'size',
            )

    sha256 = forms.CharField(
        label="SHA256", max_length=64, min_length=64, required=True)

    size = forms.IntegerField(
        label="Size", required=True)

    def __init__(self, *args, **kwargs):
        super(BootResourceNoContentForm, self).__init__(*args, **kwargs)
        # Remove content field, as this form does not use it
        del self.fields['content']

    def create_resource_file(self, resource_set, data):
        """Creates a new `BootResourceFile` on the given resource set."""
        filetype = self.get_resource_filetype(data['filetype'])
        sha256 = data['sha256']
        total_size = data['size']
        largefile = LargeFile.objects.get_file(sha256)
        if largefile is not None:
            if total_size != largefile.total_size:
                raise ValidationError(
                    "File already exists with sha256 that is of "
                    "different size.")
        else:
            # Create an empty large object. It must be opened and closed
            # for the object to be created in the database.
            largeobject = LargeObjectFile()
            largeobject.open().close()
            largefile = LargeFile.objects.create(
                sha256=sha256, total_size=total_size,
                content=largeobject)
        return BootResourceFile.objects.create(
            resource_set=resource_set, largefile=largefile,
            filename=filetype, filetype=filetype)


class ClaimIPForm(Form):
    """Form used to claim an IP address."""
    ip_address = forms.GenericIPAddressField(required=False)


class ClaimIPForMACForm(ClaimIPForm):
    """Form used to claim an IP address for a device or node."""
    mac_address = MACAddressFormField(required=False)


class ReleaseIPForm(Form):
    """Form used to release a device IP address."""
    address = forms.GenericIPAddressField(required=False)

    # unfortunately, we aren't consistent; some APIs just call this "ip"
    ip = forms.GenericIPAddressField(required=False)


class UUID4Field(forms.RegexField):
    """Validates a valid uuid version 4."""

    def __init__(self, *args, **kwargs):
        regex = (
            r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?4[0-9a-fA-F]{3}-?"
            r"[89abAB][0-9a-fA-F]{3}-?[0-9a-fA-F]{12}"
            )
        kwargs['min_length'] = 32
        kwargs['max_length'] = 36
        super(UUID4Field, self).__init__(regex, *args, **kwargs)


class AbsolutePathField(forms.RegexField):
    """Validates an absolute path."""

    def __init__(self, *args, **kwargs):
        regex = r"^(?:/[^/]*)*$"
        kwargs['min_length'] = 1

        #
        # XXX: Below, max_length was derived from linux/limits.h where it is
        # defined as PATH_MAX = 4096. 4096 includes the nul terminator, so the
        # maximum string length is only 4095 since Python does not count the
        # nul terminator.
        #
        # PATH_MAX, however, refers to *bytes* AND it may not be that useful -
        # http://insanecoding.blogspot.co.uk/2007/11/pathmax-simply-isnt.html
        # - so 4095 appears to actually be an arbitrary limit that may be too
        # large OR too small on Linux. For Windows it is almost certainly too
        # large.
        #
        kwargs['max_length'] = 4095
        super(AbsolutePathField, self).__init__(regex, *args, **kwargs)


class BytesField(forms.RegexField):
    """Validates and converts a byte value."""

    def __init__(self, *args, **kwargs):
        if "min_value" in kwargs:
            self.min_value = kwargs.pop("min_value")
        else:
            self.min_value = None
        if "max_value" in kwargs:
            self.max_value = kwargs.pop("max_value")
        else:
            self.max_value = None
        regex = r"^-?[0-9]+([KkMmGgTtPpEe]{1})?$"
        super(BytesField, self).__init__(regex, *args, **kwargs)

    def to_python(self, value):
        if value is not None:
            # Make sure the value is a string not an integer.
            value = "%s" % value
        return value

    def clean(self, value):
        value = super(BytesField, self).clean(value)
        if value is not None:
            value = machine_readable_bytes(value)

        # Run validation again, but with the min and max validators. This is
        # because the value has now been converted to an integer.
        self.validators = []
        if self.min_value is not None:
            self.validators.append(MinValueValidator(self.min_value))
        if self.max_value is not None:
            self.validators.append(MaxValueValidator(self.max_value))
        self.run_validators(value)

        return value


class FormatBlockDeviceForm(Form):
    """Form used to format a block device."""

    uuid = UUID4Field(required=False)
    fstype = forms.ChoiceField(
        choices=FILESYSTEM_FORMAT_TYPE_CHOICES, required=True)
    label = forms.CharField(required=False)

    def __init__(self, block_device, *args, **kwargs):
        super(FormatBlockDeviceForm, self).__init__(*args, **kwargs)
        self.block_device = block_device
        self.node = block_device.get_node()

    def clean(self):
        """Validate block device doesn't have a partition table."""
        # Get the clean_data, check that all of the fields we need are
        # present. If not then the form will error, so no reason to continue.
        cleaned_data = super(FormatBlockDeviceForm, self).clean()
        if 'fstype' not in cleaned_data:
            return cleaned_data
        partition_table = PartitionTable.objects.filter(
            block_device=self.block_device)
        if partition_table.exists():
            raise ValidationError(
                "Cannot format block device with a partition table.")
        return cleaned_data

    def save(self):
        """Persist the `Filesystem` into the database.

        This implementation of `save` does not support the `commit` argument.
        """
        filesystem = Filesystem.objects.filter(
            block_device=self.block_device,
            acquired=self.node.is_in_allocated_state()).first()
        if (filesystem is not None and
                filesystem.fstype not in FILESYSTEM_FORMAT_TYPE_CHOICES_DICT):
            raise ValidationError(
                "Cannot format a block device that has a filesystem "
                "type of %s." % filesystem.fstype)

        # Remove the previous format if one already exists.
        if filesystem is not None:
            filesystem.delete()

        # Create the new filesystem
        Filesystem.objects.create(
            block_device=self.block_device,
            fstype=self.cleaned_data['fstype'],
            uuid=self.cleaned_data.get('uuid', None),
            label=self.cleaned_data.get('label', None),
            acquired=self.node.is_in_allocated_state())
        return self.block_device


class AddPartitionForm(Form):
    """Form used to add a partition to block device."""

    bootable = forms.BooleanField(required=False)
    uuid = UUID4Field(required=False)

    def __init__(self, block_device, *args, **kwargs):
        super(AddPartitionForm, self).__init__(*args, **kwargs)
        self.block_device = block_device
        self.set_up_fields()

    def set_up_fields(self):
        """Create the `size` field.

        This needs to be done on the fly so that we can pass the maximum size.
        """
        self.fields['size'] = BytesField(
            min_value=MIN_PARTITION_SIZE,
            max_value=self.block_device.size,
            required=False)

    def save(self):
        partition_table, _ = PartitionTable.objects.get_or_create(
            block_device=self.block_device)
        return partition_table.add_partition(
            size=self.cleaned_data.get('size'),
            uuid=self.cleaned_data.get('uuid'),
            bootable=self.cleaned_data.get('bootable'))


class FormatPartitionForm(Form):
    """Form used to format a partition - to add a Filesystem to it."""

    uuid = UUID4Field(required=False)
    fstype = forms.ChoiceField(
        choices=FILESYSTEM_FORMAT_TYPE_CHOICES, required=True)
    label = forms.CharField(required=False)

    def __init__(self, partition, *args, **kwargs):
        super(FormatPartitionForm, self).__init__(*args, **kwargs)
        self.partition = partition
        self.node = partition.get_node()

    def save(self):
        """Add the Filesystem to the partition.

        This implementation of `save` does not support the `commit` argument.
        """
        filesystem = Filesystem.objects.filter(
            partition=self.partition,
            acquired=self.node.is_in_allocated_state()).first()
        if (filesystem is not None and
                filesystem.fstype not in FILESYSTEM_FORMAT_TYPE_CHOICES_DICT):
            raise ValidationError(
                "Cannot format a partition that has a filesystem "
                "type of %s." % filesystem.fstype)

        # Remove the previous format if one already exists.
        if filesystem is not None:
            filesystem.delete()

        # Create the new filesystem
        Filesystem.objects.create(
            partition=self.partition,
            fstype=self.cleaned_data['fstype'],
            uuid=self.cleaned_data.get('uuid', None),
            label=self.cleaned_data.get('label', None),
            acquired=self.node.is_in_allocated_state())
        return self.partition


class CreatePhysicalBlockDeviceForm(MAASModelForm):
    """For creating physical block device."""

    id_path = AbsolutePathField(required=False)
    size = BytesField(required=True)
    block_size = BytesField(required=True)

    class Meta:
        model = PhysicalBlockDevice
        fields = [
            "name",
            "model",
            "serial",
            "id_path",
            "size",
            "block_size",
        ]

    def __init__(self, node, *args, **kwargs):
        super(CreatePhysicalBlockDeviceForm, self).__init__(*args, **kwargs)
        self.node = node

    def save(self):
        block_device = super(
            CreatePhysicalBlockDeviceForm, self).save(commit=False)
        block_device.node = self.node
        block_device.save()
        return block_device


class UpdatePhysicalBlockDeviceForm(MAASModelForm):
    """For updating physical block device."""

    name = forms.CharField(required=False)
    id_path = AbsolutePathField(required=False)
    size = BytesField(required=False)
    block_size = BytesField(required=False)

    class Meta:
        model = PhysicalBlockDevice
        fields = [
            "name",
            "model",
            "serial",
            "id_path",
            "size",
            "block_size",
        ]


class UpdateDeployedPhysicalBlockDeviceForm(MAASModelForm):
    """For updating physical block device on deployed machine."""

    name = forms.CharField(required=False)
    id_path = AbsolutePathField(required=False)

    class Meta:
        model = PhysicalBlockDevice
        fields = [
            "name",
            "model",
            "serial",
            "id_path",
        ]


class UpdateVirtualBlockDeviceForm(MAASModelForm):
    """For updating virtual block device."""

    name = forms.CharField(required=False)
    uuid = UUID4Field(required=False)
    size = BytesField(required=False)

    class Meta:
        model = VirtualBlockDevice
        fields = [
            "name",
            "uuid",
            "size",
        ]

    def clean(self):
        cleaned_data = super(UpdateVirtualBlockDeviceForm, self).clean()
        is_logical_volume = self.instance.filesystem_group.is_lvm()
        size_has_changed = (
            'size' in self.cleaned_data and
            self.cleaned_data['size'] and
            self.cleaned_data['size'] != self.instance.size)
        if not is_logical_volume and size_has_changed:
            if 'size' in self.errors:
                del self.errors['size']
            set_form_error(
                self, 'size', 'Size cannot be changed on this device.')
        return cleaned_data


def convert_block_device_name_to_id(value):
    """Convert a block device value from an input field into the block device
    id.

    This is used when the user can provide either the ID or the name of the
    block device.

    :param value: User input value.
    :return: The block device ID or original input value if invalid.
    """
    if not value:
        return value
    try:
        value = int(value)
    except ValueError:
        try:
            value = BlockDevice.objects.get(name=value).id
        except BlockDevice.DoesNotExist:
            pass
    return value


def clean_block_device_name_to_id(field):
    """Helper to clean a block device input field.
    See `convert_block_device_name_to_id`."""
    def _convert(self):
        return convert_block_device_name_to_id(self.cleaned_data[field])
    return _convert


def clean_block_device_names_to_ids(field):
    """Helper to clean a block device multi choice input field.
    See `convert_block_device_name_to_id`."""
    def _convert(self):
        return [
            convert_block_device_name_to_id(block_device)
            for block_device in self.cleaned_data[field]
            ]
    return _convert


def convert_partition_name_to_id(value):
    """Convert a partition value from an input field into the partition id.

    This is used when the user can provide either the ID or the name of the
    partition.

    :param value: User input value.
    :return: The partition ID or original input value if invalid.
    """
    if not value:
        return value
    try:
        partition = Partition.objects.get_partition_by_id_or_name(value)
    except Partition.DoesNotExist:
        return value
    return partition.id


def clean_partition_name_to_id(field):
    """Helper to clean a partition input field.
    See `convert_partition_name_to_id`."""
    def _convert(self):
        return convert_partition_name_to_id(self.cleaned_data[field])
    return _convert


def clean_partition_names_to_ids(field):
    """Helper to clean a partition multi choice input field.
    See `convert_partition_name_to_id`."""
    def _convert(self):
        return [
            convert_partition_name_to_id(partition)
            for partition in self.cleaned_data[field]
            ]
    return _convert


def clean_cache_set_name_to_id(field):
    """Helper to clean a cache set choice input field.

    Converts the name of the cache_set to its id.
    """
    def _convert(self):
        value = self.cleaned_data[field]
        if not value:
            return value
        try:
            cache_set = CacheSet.objects.get_cache_set_by_id_or_name(
                value, self.node)
        except CacheSet.DoesNotExist:
            return value
        return cache_set.id
    return _convert


def get_cache_set_choices_for_node(node):
    """Return all the cache_set choices including id or name."""
    all_cache_sets = list(
        CacheSet.objects.get_cache_sets_for_node(node))
    return [
        (cs.id, cs.name)
        for cs in all_cache_sets
    ] + [
        (cs.name, cs.name)
        for cs in all_cache_sets
    ]


def _move_boot_disk_to_partitions(block_devices, partitions):
    """Removes the boot disk from the block_devices, creates a partition
    on the boot disk and adds it to partitions."""
    for block_device in block_devices:
        partition = block_device.create_partition_if_boot_disk()
        if partition is not None:
            block_devices.remove(block_device)
            partitions.append(partition)
            return


def _get_partitions_for_devices(block_devices):
    """Create and return partitions for specified block devices."""
    return [
        block_device.create_partition() for block_device in block_devices]


class CreateCacheSetForm(Form):
    """For validaing and saving a new Bcache Cache Set."""

    cache_device = forms.ChoiceField(required=False)
    cache_partition = forms.ChoiceField(required=False)

    clean_cache_device = clean_block_device_name_to_id('cache_device')
    clean_cache_partition = clean_partition_name_to_id('cache_partition')

    def __init__(self, node, *args, **kwargs):
        super(CreateCacheSetForm, self).__init__(*args, **kwargs)
        self.node = node
        self._set_up_field_choices()

    def clean(self):
        cleaned_data = super(CreateCacheSetForm, self).clean()
        cache_device = self.cleaned_data.get("cache_device")
        cache_partition = self.cleaned_data.get("cache_partition")
        if cache_device and cache_partition:
            raise ValidationError(
                "Cannot set both cache_device and cache_partition.")
        elif not cache_device and not cache_partition:
            raise ValidationError(
                "Either cache_device or cache_partition must be specified.")
        return cleaned_data

    def save(self):
        """Persist the bcache into the database.

        This implementation of `save` does not support the `commit` argument.
        """
        if self.cleaned_data['cache_device']:
            cache_device = BlockDevice.objects.get(
                id=self.cleaned_data['cache_device'])
            partition = cache_device.create_partition_if_boot_disk()
            if partition is not None:
                return CacheSet.objects.get_or_create_cache_set_for_partition(
                    partition)
            else:
                return (
                    CacheSet.objects.get_or_create_cache_set_for_block_device(
                        cache_device))
        elif self.cleaned_data['cache_partition']:
            cache_partition = Partition.objects.get(
                id=self.cleaned_data['cache_partition'])
            return CacheSet.objects.get_or_create_cache_set_for_partition(
                cache_partition)

    def _set_up_field_choices(self):
        """Sets up choices for `cache_device` and `cache_partition` fields."""
        # Select the unused, non-partitioned block devices of this node.
        free_block_devices = (
            BlockDevice.objects.get_free_block_devices_for_node(self.node))
        block_device_choices = [
            (bd.id, bd.name)
            for bd in free_block_devices
        ] + [
            (bd.name, bd.name)
            for bd in free_block_devices
        ]

        # Select the unused partitions of this node.
        free_partitions = list(
            Partition.objects.get_free_partitions_for_node(self.node))
        partition_choices = [
            (partition.id, partition.name)
            for partition in free_partitions
        ] + [
            (partition.name, partition.name)
            for partition in free_partitions
        ]

        self.fields['cache_device'].choices = block_device_choices
        self.fields['cache_partition'].choices = partition_choices


class UpdateCacheSetForm(Form):
    """For validaing and updating a Bcache Cache Set."""

    cache_device = forms.ChoiceField(required=False)
    cache_partition = forms.ChoiceField(required=False)

    clean_cache_device = clean_block_device_name_to_id('cache_device')
    clean_cache_partition = clean_partition_name_to_id('cache_partition')

    def __init__(self, cache_set, *args, **kwargs):
        super(UpdateCacheSetForm, self).__init__(*args, **kwargs)
        self.cache_set = cache_set
        self.node = cache_set.get_node()
        self._set_up_field_choices()

    def clean(self):
        cleaned_data = super(UpdateCacheSetForm, self).clean()
        if (self.cleaned_data.get("cache_device") and
                self.cleaned_data.get("cache_partition")):
            msg_error = "Cannot set both cache_device and cache_partition."
            set_form_error(self, "cache_device", msg_error)
            set_form_error(self, "cache_partition", msg_error)
        return cleaned_data

    def save(self):
        """Persist the bcache into the database.

        This implementation of `save` does not support the `commit` argument.
        """
        if self.cleaned_data['cache_device']:
            filesystem = self.cache_set.get_filesystem()
            filesystem.partition = None
            block_device = BlockDevice.objects.get(
                id=self.cleaned_data['cache_device'])
            partition = block_device.create_partition_if_boot_disk()
            if partition is not None:
                filesystem.partition = partition
            else:
                filesystem.block_device = block_device
            filesystem.save()
        elif self.cleaned_data['cache_partition']:
            filesystem = self.cache_set.get_filesystem()
            filesystem.block_device = None
            filesystem.partition = Partition.objects.get(
                id=self.cleaned_data['cache_partition'])
            filesystem.save()
        return self.cache_set

    def _set_up_field_choices(self):
        """Sets up choices for `cache_device` and `cache_partition` fields."""
        # Select the unused, non-partitioned block devices of this node.
        block_devices = list(
            BlockDevice.objects.get_free_block_devices_for_node(self.node))
        # Add the used block device, if its a block device
        device = self.cache_set.get_device()
        if isinstance(device, BlockDevice):
            block_devices.append(device)
        block_device_choices = [
            (bd.id, bd.name)
            for bd in block_devices
        ] + [
            (bd.name, bd.name)
            for bd in block_devices
        ]

        # Select the unused partitions of this node.
        partitions = list(
            Partition.objects.get_free_partitions_for_node(self.node))
        # Add the used partition, if its a partition.
        if isinstance(device, Partition):
            partitions.append(device)
        partition_choices = [
            (partition.id, partition.name)
            for partition in partitions
        ] + [
            (partition.name, partition.name)
            for partition in partitions
        ]

        self.fields['cache_device'].choices = block_device_choices
        self.fields['cache_partition'].choices = partition_choices


class CreateBcacheForm(Form):
    """For validaing and saving a new Bcache."""

    name = forms.CharField(required=False)
    uuid = UUID4Field(required=False)
    backing_device = forms.ChoiceField(required=False)
    backing_partition = forms.ChoiceField(required=False)
    cache_set = forms.ChoiceField(required=True)
    cache_mode = forms.ChoiceField(
        choices=CACHE_MODE_TYPE_CHOICES, required=True)

    clean_backing_device = clean_block_device_name_to_id('backing_device')
    clean_backing_partition = clean_partition_name_to_id('backing_partition')
    clean_cache_set = clean_cache_set_name_to_id('cache_set')

    def __init__(self, node, *args, **kwargs):
        super(CreateBcacheForm, self).__init__(*args, **kwargs)
        self.node = node
        self._set_up_field_choices()

    def clean(self):
        """Makes sure the Bcache is sensible."""
        cleaned_data = super(CreateBcacheForm, self).clean()
        Bcache.objects.validate_bcache_creation_parameters(
            cache_set=self.cleaned_data.get('cache_set'),
            cache_mode=self.cleaned_data.get('cache_mode'),
            backing_device=self.cleaned_data.get('backing_device'),
            backing_partition=self.cleaned_data.get('backing_partition'),
            validate_mode=False)  # Cache mode is validated by the field.
        return cleaned_data

    def save(self):
        """Persist the bcache into the database.

        This implementation of `save` does not support the `commit` argument.
        """
        backing_partition = backing_device = None
        if self.cleaned_data['backing_device']:
            backing_device = BlockDevice.objects.get(
                id=self.cleaned_data['backing_device'])
            partition = backing_device.create_partition_if_boot_disk()
            if partition is not None:
                backing_partition = partition
                backing_device = None
        elif self.cleaned_data['backing_partition']:
            backing_partition = Partition.objects.get(
                id=self.cleaned_data['backing_partition'])
        return Bcache.objects.create_bcache(
            cache_set=CacheSet.objects.get(id=self.cleaned_data['cache_set']),
            name=self.cleaned_data['name'],
            uuid=self.cleaned_data['uuid'],
            backing_device=backing_device,
            backing_partition=backing_partition,
            cache_mode=self.cleaned_data['cache_mode'])

    def _set_up_field_choices(self):
        """Sets up choices for `cache_set`, `backing_device`,
        and `backing_partition` fields."""
        # Select the unused, non-partitioned block devices of this node.
        free_block_devices = (
            BlockDevice.objects.get_free_block_devices_for_node(self.node))
        block_device_choices = [
            (bd.id, bd.name)
            for bd in free_block_devices
        ] + [
            (bd.name, bd.name)
            for bd in free_block_devices
        ]

        # Select the unused partitions of this node.
        free_partitions = list(
            Partition.objects.get_free_partitions_for_node(self.node))
        partition_choices = [
            (partition.id, partition.name)
            for partition in free_partitions
        ] + [
            (partition.name, partition.name)
            for partition in free_partitions
        ]

        self.fields['cache_set'].choices = get_cache_set_choices_for_node(
            self.node)
        self.fields['backing_device'].choices = block_device_choices
        self.fields['backing_partition'].choices = partition_choices


class UpdateBcacheForm(Form):
    """For validaing and saving an existing Bcache."""

    name = forms.CharField(required=False)
    uuid = UUID4Field(required=False)
    backing_device = forms.ChoiceField(required=False)
    backing_partition = forms.ChoiceField(required=False)
    cache_set = forms.ChoiceField(required=False)
    cache_mode = forms.ChoiceField(
        choices=CACHE_MODE_TYPE_CHOICES, required=False)

    clean_backing_device = clean_block_device_name_to_id('backing_device')
    clean_backing_partition = clean_partition_name_to_id('backing_partition')
    clean_cache_set = clean_cache_set_name_to_id('cache_set')

    def __init__(self, bcache, *args, **kwargs):
        super(UpdateBcacheForm, self).__init__(*args, **kwargs)
        self.bcache = bcache
        self.node = bcache.get_node()
        self._set_up_field_choices()

    def save(self):
        """Persist the bcache into the database.

        This implementation of `save` does not support the `commit` argument.
        """
        if self.cleaned_data['backing_device']:
            device = BlockDevice.objects.get(
                id=int(self.cleaned_data['backing_device']))
            # Remove previous cache
            self.bcache.filesystems.filter(
                fstype=FILESYSTEM_TYPE.BCACHE_BACKING).delete()
            # Create a new one on this device or on the partition on this
            # device if the device is the boot disk.
            partition = device.create_partition_if_boot_disk()
            if partition is not None:
                filesystem = Filesystem.objects.create(
                    partition=partition, fstype=FILESYSTEM_TYPE.BCACHE_BACKING)
            else:
                filesystem = Filesystem.objects.create(
                    block_device=device, fstype=FILESYSTEM_TYPE.BCACHE_BACKING)
            self.bcache.filesystems.add(filesystem)
        elif self.cleaned_data['backing_partition']:
            partition = Partition.objects.get(
                id=int(self.cleaned_data['backing_partition']))
            # Remove previous cache
            self.bcache.filesystems.filter(
                fstype=FILESYSTEM_TYPE.BCACHE_BACKING).delete()
            # Create a new one on this partition.
            self.bcache.filesystems.add(Filesystem.objects.create(
                partition=partition, fstype=FILESYSTEM_TYPE.BCACHE_BACKING))

        if self.cleaned_data['name']:
            self.bcache.name = self.cleaned_data['name']
        if self.cleaned_data['uuid']:
            self.bcache.uuid = self.cleaned_data['uuid']
        if self.cleaned_data['cache_mode']:
            self.bcache.cache_mode = self.cleaned_data['cache_mode']
        if self.cleaned_data['cache_set']:
            self.bcache.cache_set = CacheSet.objects.get(
                id=self.cleaned_data['cache_set'])

        self.bcache.save()
        return self.bcache

    def _set_up_field_choices(self):
        """Sets up choices for `cache_device`, `backing_device`,
        `cache_partition` and `backing_partition` fields."""

        # Select the unused, non-partitioned block devices of this node, append
        # the ones currently used by bcache and exclude the virtual block
        # device created by the cache.
        free_block_devices = (
            BlockDevice.objects.get_free_block_devices_for_node(self.node))
        free_block_devices = free_block_devices.exclude(
            id=self.bcache.virtual_device.id)
        current_block_devices = self.bcache.filesystems.exclude(
            block_device=None)
        block_device_choices = [
            (bd.id, bd.name)
            for bd in free_block_devices
        ] + [
            (bd.name, bd.name)
            for bd in free_block_devices
        ] + [
            (fs.block_device_id, fs.block_device.name)
            for fs in current_block_devices
        ] + [
            (fs.block_device.name, fs.block_device.name)
            for fs in current_block_devices
        ]

        # Select the unused partitions of this node, append the bcache ones (if
        # they exist).
        free_partitions = Partition.objects.get_free_partitions_for_node(
            self.node)
        current_partitions = self.bcache.filesystems.exclude(partition=None)
        partition_choices = [
            (partition.id, partition.name)
            for partition in free_partitions
        ] + [
            (partition.name, partition.name)
            for partition in free_partitions
        ] + [
            (fs.partition_id, fs.partition.name)
            for fs in current_partitions
        ] + [
            (fs.partition.name, fs.partition.name)
            for fs in current_partitions
        ]

        self.fields['backing_device'].choices = block_device_choices
        self.fields['backing_partition'].choices = partition_choices
        self.fields['cache_set'].choices = get_cache_set_choices_for_node(
            self.node)


class CreateRaidForm(Form):
    """For validating and saving a new RAID."""

    name = forms.CharField(required=False)
    uuid = UUID4Field(required=False)
    level = forms.ChoiceField(
        choices=FILESYSTEM_GROUP_RAID_TYPE_CHOICES, required=True)
    block_devices = forms.MultipleChoiceField(required=False)
    partitions = forms.MultipleChoiceField(required=False)
    spare_devices = forms.MultipleChoiceField(required=False)
    spare_partitions = forms.MultipleChoiceField(required=False)

    clean_block_devices = clean_block_device_names_to_ids('block_devices')
    clean_partitions = clean_partition_names_to_ids('partitions')
    clean_spare_devices = clean_block_device_names_to_ids('spare_devices')
    clean_spare_partitions = clean_partition_names_to_ids('spare_partitions')

    def _set_up_field_choices(self):
        """Sets up the `block_devices`, `partition`, `spare_devices` and
        `spare_partitions` fields.

        This needs to be done on the fly so that we can pass a dynamic list of
        partitions and block devices that fit this node.

        """
        # Select the unused, non-partitioned block devices of this node.
        free_block_devices = (
            BlockDevice.objects.get_free_block_devices_for_node(self.node))
        block_device_choices = [
            (bd.id, bd.name)
            for bd in free_block_devices
        ] + [
            (bd.name, bd.name)
            for bd in free_block_devices
        ]

        # Select the unused partitions of this node.
        free_partitions = Partition.objects.get_free_partitions_for_node(
            self.node)
        partition_choices = [
            (partition.id, partition.name)
            for partition in free_partitions
        ] + [
            (partition.name, partition.name)
            for partition in free_partitions
        ]

        self.fields['block_devices'].choices = block_device_choices
        self.fields['partitions'].choices = partition_choices
        self.fields['spare_devices'].choices = block_device_choices
        self.fields['spare_partitions'].choices = partition_choices

    def __init__(self, node, *args, **kwargs):
        super(CreateRaidForm, self).__init__(*args, **kwargs)
        self.node = node
        self._set_up_field_choices()

    def clean(self):
        cleaned_data = super(CreateRaidForm, self).clean()
        # It is not possible to create a RAID without any devices or
        # partitions, but we catch this situation here in order to provide a
        # clearer error message.
        if ('block_devices' in cleaned_data and
                'partitions' in cleaned_data and len(
                    cleaned_data['block_devices'] +
                    cleaned_data['partitions']) == 0):
            raise ValidationError(
                'At least one block device or partition must be added to the '
                'array.')
        return cleaned_data

    def save(self):
        """Persist the RAID into the database.

        This implementation of `save` does not support the `commit` argument.
        """
        block_devices = list(BlockDevice.objects.filter(
            id__in=self.cleaned_data['block_devices']))
        partitions = list(Partition.objects.filter(
            id__in=self.cleaned_data['partitions']))
        spare_devices = list(BlockDevice.objects.filter(
            id__in=self.cleaned_data['spare_devices']))
        spare_partitions = list(Partition.objects.filter(
            id__in=self.cleaned_data['spare_partitions']))
        boot_disk = self.node.get_boot_disk()
        if boot_disk.id in chain(
                self.cleaned_data['block_devices'],
                self.cleaned_data['spare_devices']):
            # if the raid is bootable, create partitions for all disks
            partitions.extend(_get_partitions_for_devices(block_devices))
            spare_partitions.extend(_get_partitions_for_devices(spare_devices))
            # don't use raw devices anymore
            block_devices = []
            spare_devices = []

        return RAID.objects.create_raid(
            name=self.cleaned_data['name'],
            level=self.cleaned_data['level'],
            uuid=self.cleaned_data['uuid'],
            block_devices=block_devices,
            partitions=partitions,
            spare_devices=spare_devices,
            spare_partitions=spare_partitions
        )


class UpdateRaidForm(Form):
    """Form for updating a RAID."""

    name = forms.CharField(required=False)
    uuid = UUID4Field(required=False)

    add_block_devices = forms.MultipleChoiceField(required=False)
    add_partitions = forms.MultipleChoiceField(required=False)
    add_spare_devices = forms.MultipleChoiceField(required=False)
    add_spare_partitions = forms.MultipleChoiceField(required=False)

    remove_block_devices = forms.MultipleChoiceField(required=False)
    remove_partitions = forms.MultipleChoiceField(required=False)
    remove_spare_devices = forms.MultipleChoiceField(required=False)
    remove_spare_partitions = forms.MultipleChoiceField(required=False)

    clean_add_block_devices = clean_block_device_names_to_ids(
        'add_block_devices')
    clean_add_partitions = clean_partition_names_to_ids(
        'add_partitions')
    clean_add_spare_devices = clean_block_device_names_to_ids(
        'add_spare_devices')
    clean_add_spare_partitions = clean_partition_names_to_ids(
        'add_spare_partitions')

    clean_remove_block_devices = clean_block_device_names_to_ids(
        'remove_block_devices')
    clean_remove_partitions = clean_partition_names_to_ids(
        'remove_partitions')
    clean_remove_spare_devices = clean_block_device_names_to_ids(
        'remove_spare_devices')
    clean_remove_spare_partitions = clean_partition_names_to_ids(
        'remove_spare_partitions')

    def __init__(self, raid, *args, **kwargs):
        super(UpdateRaidForm, self).__init__(*args, **kwargs)
        self.raid = raid
        self.set_up_field_choices()

    def set_up_field_choices(self):
        """Sets up the `add_block_devices`, `add_partitions`,
        `add_spare_devices`, add_spare_partitions`, `remove_block_devices`,
        `remove_partition`, `remove_spare_devices`, `remove_spare_partitions`
        fields.

        This needs to be done on the fly so that we can pass a dynamic list of
        partitions and block devices that fit this node.

        """
        node = self.raid.get_node()

        # Select the unused, non-partitioned block devices of this node.
        free_block_devices = (
            BlockDevice.objects.get_free_block_devices_for_node(node))
        add_block_device_choices = [
            (bd.id, bd.name)
            for bd in free_block_devices
        ] + [
            (bd.name, bd.name)
            for bd in free_block_devices
        ]

        # Select the unused partitions of this node.
        free_partitions = Partition.objects.get_free_partitions_for_node(node)
        add_partition_choices = [
            (p.id, p.name)
            for p in free_partitions
        ] + [
            (p.name, p.name)
            for p in free_partitions
        ]

        # Select the used block devices of this RAID.
        current_block_devices = self.raid.filesystems.exclude(
            block_device=None)
        remove_block_device_choices = [
            (fs.block_device.id, fs.block_device.name)
            for fs in current_block_devices
        ] + [
            (fs.block_device.name, fs.block_device.name)
            for fs in current_block_devices
        ]

        # Select the used partitions of this RAID.
        current_partitions = self.raid.filesystems.exclude(partition=None)
        remove_partition_choices = [
            (fs.partition.id, fs.partition.name)
            for fs in current_partitions
        ] + [
            (fs.partition.name, fs.partition.name)
            for fs in current_partitions
        ]

        # Sets up the choices for additive fields.
        self.fields['add_block_devices'].choices = add_block_device_choices
        self.fields['add_partitions'].choices = add_partition_choices
        self.fields['add_spare_devices'].choices = add_block_device_choices
        self.fields['add_spare_partitions'].choices = add_partition_choices

        # Sets up the choices for removal fields.
        self.fields['remove_block_devices'].choices = (
            remove_block_device_choices)
        self.fields['remove_partitions'].choices = remove_partition_choices
        self.fields['remove_spare_devices'].choices = (
            remove_block_device_choices)
        self.fields['remove_spare_partitions'].choices = (
            remove_partition_choices)

    def save(self):
        """Save updates to the RAID.

        This implementation of `save` does not support the `commit` argument.
        """

        current_block_device_ids = [
            fs.block_device.id for fs in self.raid.filesystems.filter(
                fstype=FILESYSTEM_TYPE.RAID).exclude(block_device=None)
        ]
        current_spare_device_ids = [
            fs.block_device.id for fs in self.raid.filesystems.filter(
                fstype=FILESYSTEM_TYPE.RAID_SPARE).exclude(block_device=None)
        ]
        current_partition_ids = [
            fs.partition.id for fs in self.raid.filesystems.filter(
                fstype=FILESYSTEM_TYPE.RAID).exclude(partition=None)
        ]
        current_spare_partition_ids = [
            fs.partition.id for fs in self.raid.filesystems.filter(
                fstype=FILESYSTEM_TYPE.RAID_SPARE).exclude(partition=None)
        ]

        for device_id in (
                self.cleaned_data['remove_block_devices'] +
                self.cleaned_data['remove_spare_devices']):
            if (device_id in current_block_device_ids +
                    current_spare_device_ids):
                self.raid.remove_device(BlockDevice.objects.get(id=device_id))

        for partition_id in (
                self.cleaned_data['remove_partitions'] +
                self.cleaned_data['remove_spare_partitions']):
            if (partition_id in current_partition_ids +
                    current_spare_partition_ids):
                self.raid.remove_partition(
                    Partition.objects.get(id=partition_id))

        for device_id in self.cleaned_data['add_block_devices']:
            if device_id not in current_block_device_ids:
                block_device = BlockDevice.objects.get(id=device_id)
                partition = block_device.create_partition_if_boot_disk()
                if partition is not None:
                    self.raid.add_partition(partition, FILESYSTEM_TYPE.RAID)
                else:
                    self.raid.add_device(block_device, FILESYSTEM_TYPE.RAID)

        for device_id in self.cleaned_data['add_spare_devices']:
            if device_id not in current_block_device_ids:
                block_device = BlockDevice.objects.get(id=device_id)
                partition = block_device.create_partition_if_boot_disk()
                if partition is not None:
                    self.raid.add_partition(
                        partition, FILESYSTEM_TYPE.RAID_SPARE)
                else:
                    self.raid.add_device(
                        block_device, FILESYSTEM_TYPE.RAID_SPARE)

        for partition_id in self.cleaned_data['add_partitions']:
            if partition_id not in current_partition_ids:
                self.raid.add_partition(
                    Partition.objects.get(id=partition_id),
                    FILESYSTEM_TYPE.RAID)

        for partition_id in self.cleaned_data['add_spare_partitions']:
            if partition_id not in current_partition_ids:
                self.raid.add_partition(
                    Partition.objects.get(id=partition_id),
                    FILESYSTEM_TYPE.RAID_SPARE)

        # The simple attributes
        if 'name' in self.cleaned_data and self.cleaned_data['name']:
            self.raid.name = self.cleaned_data['name']

        if 'uuid' in self.cleaned_data and self.cleaned_data['uuid']:
            self.raid.uuid = self.cleaned_data['uuid']

        self.raid.save()
        return self.raid


class CreateVolumeGroupForm(Form):
    """For validating and saving a new volume group."""

    name = forms.CharField(required=True)
    uuid = UUID4Field(required=False)
    block_devices = forms.MultipleChoiceField(required=False)
    partitions = forms.MultipleChoiceField(required=False)

    clean_block_devices = clean_block_device_names_to_ids('block_devices')
    clean_partitions = clean_partition_names_to_ids('partitions')

    def __init__(self, node, *args, **kwargs):
        super(CreateVolumeGroupForm, self).__init__(*args, **kwargs)
        self.node = node
        self.set_up_choice_fields()

    def set_up_choice_fields(self):
        """Sets up the choice fields.

        This needs to be done on the fly so that we can pass a dynamic list of
        partitions and block devices that fit this node.
        """
        # Select the unused, non-partitioned block devices of this node.
        free_block_devices = (
            BlockDevice.objects.get_free_block_devices_for_node(self.node))
        self.fields['block_devices'].choices = [
            (bd.id, bd.name)
            for bd in free_block_devices
        ] + [
            (bd.name, bd.name)
            for bd in free_block_devices
        ]
        # Select the unused partitions of this node.
        free_partitions = Partition.objects.get_free_partitions_for_node(
            self.node)
        self.fields['partitions'].choices = [
            (partition.id, partition.name)
            for partition in free_partitions
        ] + [
            (partition.name, partition.name)
            for partition in free_partitions
        ]

    def clean(self):
        """Validate that at least one block device or partition is given."""
        cleaned_data = super(CreateVolumeGroupForm, self).clean()
        if "name" not in cleaned_data:
            return cleaned_data
        has_block_devices = (
            "block_devices" in cleaned_data and
            len(cleaned_data["block_devices"]) > 0)
        has_partitions = (
            "partitions" in cleaned_data and
            len(cleaned_data["partitions"]) > 0)
        has_block_device_and_partition_errors = (
            "block_devices" in self._errors or "partitions" in self._errors)
        if (not has_block_devices and
                not has_partitions and
                not has_block_device_and_partition_errors):
            raise ValidationError(
                "At least one valid block device or partition is required.")
        return cleaned_data

    def save(self):
        """Persist the `VolumeGroup` into the database.

        This implementation of `save` does not support the `commit` argument.
        """
        block_devices = list(BlockDevice.objects.filter(
            id__in=self.cleaned_data['block_devices']))
        partitions = list(Partition.objects.filter(
            id__in=self.cleaned_data['partitions']))
        _move_boot_disk_to_partitions(block_devices, partitions)
        return VolumeGroup.objects.create_volume_group(
            name=self.cleaned_data['name'],
            uuid=self.cleaned_data.get('uuid'),
            block_devices=block_devices,
            partitions=partitions)


class UpdateVolumeGroupForm(Form):
    """For validating and updating a new volume group."""

    name = forms.CharField(required=False)
    uuid = UUID4Field(required=False)
    add_block_devices = forms.MultipleChoiceField(required=False)
    remove_block_devices = forms.MultipleChoiceField(required=False)
    add_partitions = forms.MultipleChoiceField(required=False)
    remove_partitions = forms.MultipleChoiceField(required=False)

    clean_add_block_devices = clean_block_device_names_to_ids(
        'add_block_devices')
    clean_remove_block_devices = clean_block_device_names_to_ids(
        'remove_block_devices')
    clean_add_partitions = clean_partition_names_to_ids(
        'add_partitions')
    clean_remove_partitions = clean_partition_names_to_ids(
        'remove_partitions')

    def __init__(self, volume_group, *args, **kwargs):
        super(UpdateVolumeGroupForm, self).__init__(*args, **kwargs)
        self.volume_group = volume_group
        self.set_up_choice_fields()

    def set_up_choice_fields(self):
        """Sets up the choice fields.

        This needs to be done on the fly so that we can pass a dynamic list of
        partitions and block devices that fit this node.
        """
        node = self.volume_group.get_node()
        # Select the unused, non-partitioned block devices of this node.
        free_block_devices = (
            BlockDevice.objects.get_free_block_devices_for_node(node))
        self.fields['add_block_devices'].choices = [
            (bd.id, bd.name)
            for bd in free_block_devices
        ] + [
            (bd.name, bd.name)
            for bd in free_block_devices
        ]
        # Select the unused partitions of this node.
        free_partitions = Partition.objects.get_free_partitions_for_node(
            node)
        self.fields['add_partitions'].choices = [
            (partition.id, partition.name)
            for partition in free_partitions
        ] + [
            (partition.name, partition.name)
            for partition in free_partitions
        ]
        # Select the block devices in the volume group.
        used_block_devices = (
            BlockDevice.objects.get_block_devices_in_filesystem_group(
                self.volume_group))
        self.fields['remove_block_devices'].choices = [
            (bd.id, bd.name)
            for bd in used_block_devices
        ] + [
            (bd.name, bd.name)
            for bd in used_block_devices
        ]
        # Select the current partitions in the volume group.
        used_partitions = (
            Partition.objects.get_partitions_in_filesystem_group(
                self.volume_group))
        self.fields['remove_partitions'].choices = [
            (partition.id, partition.name)
            for partition in used_partitions
        ] + [
            (partition.name, partition.name)
            for partition in used_partitions
        ]

    def save(self):
        """Update the `VolumeGroup`.

        This implementation of `save` does not support the `commit` argument.
        """
        if 'name' in self.cleaned_data and self.cleaned_data['name']:
            self.volume_group.name = self.cleaned_data['name']
        if 'uuid' in self.cleaned_data and self.cleaned_data['uuid']:
            self.volume_group.uuid = self.cleaned_data['uuid']

        # Create the new list of block devices.
        add_block_device_ids = self.cleaned_data['add_block_devices']
        remove_block_device_ids = self.cleaned_data['remove_block_devices']
        block_devices = (
            BlockDevice.objects.get_block_devices_in_filesystem_group(
                self.volume_group))
        block_devices = [
            block_device
            for block_device in block_devices
            if block_device.id not in remove_block_device_ids
            ]
        block_devices = block_devices + list(
            BlockDevice.objects.filter(id__in=add_block_device_ids))

        # Create the new list of partitions.
        add_partition_ids = self.cleaned_data['add_partitions']
        remove_partition_ids = self.cleaned_data['remove_partitions']
        partitions = (
            Partition.objects.get_partitions_in_filesystem_group(
                self.volume_group))
        partitions = [
            partition
            for partition in partitions
            if partition.id not in remove_partition_ids
            ]
        partitions = partitions + list(
            Partition.objects.filter(id__in=add_partition_ids))

        # Move the boot disk to the partitions if it exists.
        _move_boot_disk_to_partitions(block_devices, partitions)

        # Update the block devices and partitions in the volume group.
        self.volume_group.update_block_devices_and_partitions(
            block_devices, partitions)
        self.volume_group.save()
        return self.volume_group


class CreateLogicalVolumeForm(Form):
    """Form used to add a logical volume to a volume group."""

    name = forms.CharField(required=True)
    uuid = UUID4Field(required=False)

    def __init__(self, volume_group, *args, **kwargs):
        super(CreateLogicalVolumeForm, self).__init__(*args, **kwargs)
        self.volume_group = volume_group
        self.set_up_fields()

    def set_up_fields(self):
        """Create the `size` fields.

        This needs to be done on the fly so that we can pass the maximum size.
        """
        self.fields['size'] = BytesField(
            min_value=MIN_BLOCK_DEVICE_SIZE,
            max_value=self.volume_group.get_lvm_free_space(),
            required=True)

    def clean(self):
        """Validate that at least one block device or partition is given."""
        cleaned_data = super(CreateLogicalVolumeForm, self).clean()
        if self.volume_group.get_lvm_free_space() < MIN_BLOCK_DEVICE_SIZE:
            # Remove the size errors. They are confusing because the
            # minimum is larger than the maximum.
            if "size" in self._errors:
                del self._errors["size"]
            raise ValidationError(
                "Volume group (%s) cannot hold any more logical volumes, "
                "because it doesn't have enough free space." % (
                    self.volume_group.name))
        return cleaned_data

    def save(self):
        return self.volume_group.create_logical_volume(
            name=self.cleaned_data['name'],
            uuid=self.cleaned_data.get('uuid'),
            size=self.cleaned_data['size'])
