# -*- coding: utf-8 -*-
# pylint: disable=E1101
# pylint: disable=W0621
import os
import uuid

import fauxfactory
import pytest
from six.moves.urllib.parse import urljoin
from wait_for import wait_for
from widgetastic.exceptions import MoveTargetOutOfBoundsException
from wrapanapi import VmState

from cfme import test_requirements
from cfme.cloud.provider import CloudProvider
from cfme.cloud.provider.azure import AzureProvider
from cfme.cloud.provider.ec2 import EC2Provider
from cfme.cloud.provider.gce import GCEProvider
from cfme.cloud.provider.openstack import OpenStackProvider
from cfme.cloud.provider.openstack import RHOSEndpoint
from cfme.common.provider_views import CloudProviderAddView
from cfme.common.provider_views import CloudProvidersView
from cfme.fixtures.provider import enable_provider_regions
from cfme.markers.env_markers.provider import ONE
from cfme.rest.gen_data import arbitration_profiles as _arbitration_profiles
from cfme.utils import appliance
from cfme.utils import conf
from cfme.utils import ssh
from cfme.utils.appliance.implementations.ui import navigate_to
from cfme.utils.conf import credentials
from cfme.utils.generators import random_vm_name
from cfme.utils.providers import list_providers
from cfme.utils.providers import ProviderFilter
from cfme.utils.rest import assert_response
from cfme.utils.update import update
from cfme.utils.wait import wait_for_decorator

pytestmark = [pytest.mark.provider([CloudProvider], scope="function")]

# path, where powershell scripts are located and where cfme image is downloaded,
# needed for test_create_azure_vm_from_azure_image
SPACE = '/mnt/space/'


@pytest.fixture(scope='function')
def enable_regions(provider):
    enable_provider_regions(provider)


@pytest.fixture(scope='function')
def az_pwsh_vm(appliance):
    """
    azure_pwsh contains powershell and necessary scripts to upload vhd, create VM, get ip of the
    resource and delete the VM.
    Find the provider that contains that template.

    The example of the configuration can be found in data/az_pwsh_cloudinit.cfg
    """
    filter_pwsh_template = ProviderFilter(required_fields=[['templates', 'powershell_vm']])
    providers = list_providers(filters=[filter_pwsh_template])
    if not providers:
        pytest.skip("There's no provider that contains a template with powershell")

    # If there's more than 1 provider that has the template, we select the first
    provider = providers[0]

    vm_name = random_vm_name(context="pwsh")
    pwsh_vm = provider.data.templates.powershell_vm.name

    collection = provider.appliance.provider_based_collection(provider)

    try:
        vm = collection.instantiate(vm_name, provider, pwsh_vm)
        vm.create_on_provider(allow_skip="default")
    except IndexError:
        from cfme.exceptions import ItemNotFound
        raise ItemNotFound('VM with powershell not found!')

    vm.mgmt.ensure_state(VmState.RUNNING)

    @wait_for_decorator(timeout="10m", delay=5)
    def get_ip_address():
        ip = vm.ip_address
        return ip is not None
    yield vm

    vm.cleanup_on_provider()


@pytest.fixture
def pwsh_ssh(az_pwsh_vm):
    """Provide vm_ssh_client for ssh operations in the test."""
    with ssh.SSHClient(hostname=az_pwsh_vm.ip_address,
                       username=credentials['host_default']['username'],
                       password=credentials['host_default']['password']) as vm_ssh_client:
        yield vm_ssh_client


@pytest.fixture
def connect_az_account(pwsh_ssh):
    """
    Connect to Azure account to run further scripts, see
    https://docs.microsoft.com/en-us/powershell/azure/authenticate-azureps
    """
    path_script = os.path.join(SPACE, 'connect_account.ps1')
    connect = pwsh_ssh.run_command("pwsh {}".format(path_script), timeout=180)
    assert connect.success, "Failed to connect to Azure account"


@pytest.fixture(scope='function')
def cfme_vhd(appliance, pwsh_ssh):
    path_script = os.path.join(SPACE, 'get_ip.ps1')
    ip_of_recourse = pwsh_ssh.run_command(
        r'pwsh {}| grep -oE "([0-9]{{1,3}}\.){{3}}[0-9]{{1,3}}"'.format(path_script),
        timeout=60).output.strip()
    if ip_of_recourse is not None:
        pytest.skip("The resource is taken by some other VM in Azure")
    stream = appliance.version.stream()
    try:
        # need to add the trailing slash for urljoin to work correctly
        url = '{}/'.format(conf.cfme_data['basic_info']['cfme_images_url'][stream])
    except KeyError:
        pytest.skip("Skipping since no such key found in yaml")

    image = pwsh_ssh.run_command(
        """wget -qO- {url} | grep -Po '(?<=href=")[^"]*' | grep azure""".format(url=url),
        timeout=30).output.strip()
    image_url = urljoin(url, image)
    pwsh_ssh.run_command("wget {image} -P {dest}".format(dest=SPACE, image=image_url),
                         timeout=180)

    # unpack the archive
    vhd = image.replace('zip', 'vhd')
    pwsh_ssh.run_command("unzip {} -d {}".format(os.path.join(SPACE, image), SPACE),
                         timeout=15 * 60)
    yield vhd

    pwsh_ssh.run_command("rm -f {}".format(os.path.join(SPACE, image)), timeout=180)
    pwsh_ssh.run_command("rm -f {}".format(os.path.join(SPACE, vhd)), timeout=180)


@pytest.fixture(scope='function')
def upload_image_to_azure(cfme_vhd, pwsh_ssh):
    path_script = os.path.join(SPACE, 'upload_vhd.ps1')
    pwsh_ssh.run_command(
        r"""sed -i '1s/.*/$BlobNameSource = "{vhd}"/' {script}"""
            .format(script=path_script, vhd=cfme_vhd), timeout=30)

    pwsh_ssh.run_command("pwsh {}".format(path_script), timeout=15 * 60)


@pytest.fixture(scope='function')
def vm_ip(cfme_vhd, pwsh_ssh):
    # Create VM in Azure
    path_script = os.path.join(SPACE, 'create_vm.ps1')
    pwsh_ssh.run_command(
        r"""sed -i '1s/.*/$BlobNameSource = "{vhd}"/' {script} &&
        sed -i '2s/.*/$BlobNameDest = "{b_dest}"/' {script} &&
        sed -i '3s/.*/$VMName = "{name}"/' {script}""".format(
            script=path_script,
            vhd=cfme_vhd,
            b_dest=cfme_vhd.replace('azure', 'test'),
            name=cfme_vhd.replace('.x86_64.vhd', '-vm')),
        timeout=20)

    pwsh_ssh.run_command("pwsh {}".format(path_script), timeout=600)

    # get the ip of the resource
    path_get_ip = os.path.join(SPACE, 'get_ip.ps1')
    ip = pwsh_ssh.run_command(
        r'pwsh {}| grep -oE "([0-9]{{1,3}}\.){{3}}[0-9]{{1,3}}"'.format(path_get_ip),
        timeout=60).output.strip()
    yield ip

    # Delete the VM
    with pwsh_ssh:
        pwsh_ssh.run_command(
            r"""sed -i '1s/.*/$VMName = "{name}"/' {script}""".format(
                script=path_script,
                name=cfme_vhd.replace('.x86_64.vhd', '-vm')),
            timeout=20)

        pwsh_ssh.run_command("pwsh {}".format(path_script), timeout=180)


@pytest.mark.tier(3)
@test_requirements.discovery
def test_add_cancelled_validation_cloud(request, appliance):
    """Tests that the flash message is correct when add is cancelled.

    Polarion:
        assignee: pvala
        casecomponent: Cloud
        caseimportance: medium
        initialEstimate: 1/16h
    """
    collection = appliance.collections.cloud_providers
    prov = collection.instantiate(prov_class=EC2Provider)
    request.addfinalizer(prov.delete_if_exists)
    try:
        prov.create(cancel=True)
    except MoveTargetOutOfBoundsException:
        # TODO: Remove once fixed 1475303
        prov.create(cancel=True)
    view = prov.browser.create_view(CloudProvidersView)
    view.flash.assert_success_message('Add of Cloud Provider was cancelled by the user')


@pytest.mark.tier(3)
@pytest.mark.usefixtures('has_no_cloud_providers')
@test_requirements.discovery
def test_cloud_provider_add_with_bad_credentials(provider, enable_regions):
    """ Tests provider add with bad credentials

    Metadata:
        test_flag: crud

    Polarion:
        assignee: pvala
        casecomponent: Cloud
        caseimportance: high
        initialEstimate: 1/6h
    """
    default_credentials = provider.default_endpoint.credentials

    # default settings
    flash = 'Login failed due to a bad username or password.'
    default_credentials.principal = "bad"
    default_credentials.secret = 'notyourday'

    if provider.one_of(AzureProvider):
        flash = (
            "Credential validation was not successful: Incorrect credentials - "
            "check your Azure Client ID and Client Key"
        )
        default_credentials.principal = str(uuid.uuid4())
        default_credentials.secret = 'notyourday'
    elif provider.one_of(GCEProvider):
        flash = 'Credential validation was not successful: Invalid Google JSON key'
        default_credentials.service_account = '{"test": "bad"}'
    elif provider.one_of(OpenStackProvider):
        for endp_name in provider.endpoints.keys():
            if endp_name != 'default':
                del provider.endpoints[endp_name]

    with pytest.raises(Exception, match=flash):
        provider.create(validate_credentials=True)


@pytest.mark.rhel_testing
@pytest.mark.tier(1)
@pytest.mark.smoke
@pytest.mark.usefixtures('has_no_cloud_providers')
@test_requirements.discovery
def test_cloud_provider_crud(provider, enable_regions):
    """ Tests provider add with good credentials

    Metadata:
        test_flag: crud

    Polarion:
        assignee: pvala
        casecomponent: Cloud
        caseimportance: high
        initialEstimate: 1/3h
    """
    provider.create()
    provider.validate_stats(ui=True)

    old_name = provider.name
    with update(provider):
        provider.name = str(uuid.uuid4())  # random uuid

    with update(provider):
        provider.name = old_name  # old name

    provider.delete(cancel=False)
    provider.wait_for_delete()


@pytest.mark.tier(3)
@test_requirements.discovery
def test_type_required_validation_cloud(request, appliance):
    """Test to validate type while adding a provider

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        caseimportance: high
        initialEstimate: 1/10h
    """
    collection = appliance.collections.cloud_providers
    view = navigate_to(collection, 'Add')
    view.fill({'name': 'foo'})
    assert not view.add.active


@pytest.mark.tier(3)
@test_requirements.discovery
def test_name_required_validation_cloud(request, appliance):
    """Tests to validate the name while adding a provider

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        caseimportance: high
        initialEstimate: 1/15h
    """
    collection = appliance.collections.cloud_providers
    prov = collection.instantiate(prov_class=EC2Provider,
                                  name=None,
                                  region='US East (Northern Virginia)')
    request.addfinalizer(prov.delete_if_exists)
    with pytest.raises(AssertionError):
        prov.create()
    view = prov.create_view(CloudProviderAddView)
    assert view.name.help_block == "Required"
    assert not view.add.active


@pytest.mark.tier(3)
def test_region_required_validation(request, soft_assert, appliance):
    """Tests to validate the region while adding a provider

    Polarion:
        assignee: anikifor
        caseimportance: low
        casecomponent: WebUI
        initialEstimate: 1/6h
    """
    collection = appliance.collections.cloud_providers
    prov = collection.instantiate(prov_class=EC2Provider, name=fauxfactory.gen_alphanumeric(5),
                                  region=None)

    request.addfinalizer(prov.delete_if_exists)
    with pytest.raises(AssertionError):
        prov.create()
        view = prov.create_view(CloudProviderAddView)
        soft_assert(view.region.help_block == "Required")


@pytest.mark.tier(3)
@test_requirements.discovery
def test_host_name_required_validation_cloud(request, appliance):
    """Test to validate the hostname while adding a provider

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        caseimportance: high
        initialEstimate: 1/15h
    """
    endpoint = RHOSEndpoint(hostname=None,
                            ip_address=fauxfactory.gen_ipaddr(prefix=[10]),
                            security_protocol=None)
    collection = appliance.collections.cloud_providers
    prov = collection.instantiate(prov_class=OpenStackProvider,
                                  name=fauxfactory.gen_alphanumeric(5),
                                  endpoints=endpoint)

    request.addfinalizer(prov.delete_if_exists)
    # It must raise an exception because it keeps on the form
    with pytest.raises(AssertionError):
        prov.create()
    endpoints = prov.create_view(prov.endpoints_form)
    assert endpoints.default.hostname.help_block == "Required"


@pytest.mark.tier(3)
@test_requirements.general_ui
def test_api_port_blank_validation(request, appliance):
    """Test to validate blank api port while adding a provider

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        caseimportance: low
        initialEstimate: 1/6h
    """
    endpoint = RHOSEndpoint(hostname=fauxfactory.gen_alphanumeric(5),
                            ip_address=fauxfactory.gen_ipaddr(prefix=[10]),
                            api_port='',
                            security_protocol='Non-SSL')
    collection = appliance.collections.cloud_providers
    prov = collection.instantiate(prov_class=OpenStackProvider,
                                  name=fauxfactory.gen_alphanumeric(5),
                                  endpoints=endpoint)

    request.addfinalizer(prov.delete_if_exists)
    # It must raise an exception because it keeps on the form
    with pytest.raises(AssertionError):
        prov.create()
    endpoints = prov.create_view(prov.endpoints_form)
    assert endpoints.default.api_port.help_block == "Required"


@pytest.mark.tier(3)
@test_requirements.discovery
def test_name_max_character_validation_cloud(request, cloud_provider):
    """Test to validate that provider can have up to 255 characters in name

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        caseimportance: medium
        initialEstimate: 1/15h
    """
    request.addfinalizer(lambda: cloud_provider.delete_if_exists(cancel=False))
    name = fauxfactory.gen_alphanumeric(255)
    with update(cloud_provider):
        cloud_provider.name = name
    assert cloud_provider.exists


@pytest.mark.tier(3)
def test_hostname_max_character_validation_cloud(appliance):
    """Test to validate max character for hostname field

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        caseimportance: high
        initialEstimate: 1/15h
    """
    endpoint = RHOSEndpoint(hostname=fauxfactory.gen_alphanumeric(256),
                            api_port=None,
                            security_protocol=None)
    collection = appliance.collections.cloud_providers
    prov = collection.instantiate(prov_class=OpenStackProvider,
                                  name=fauxfactory.gen_alphanumeric(5),
                                  endpoints=endpoint)
    try:
        prov.create()
    except MoveTargetOutOfBoundsException:
        # TODO: Remove once fixed 1475303
        prov.create()
    except AssertionError:
        endpoints = prov.create_view(prov.endpoints_form)
        assert endpoints.default.hostname.value == prov.hostname[0:255]


@pytest.mark.tier(3)
@test_requirements.discovery
def test_api_port_max_character_validation_cloud(appliance):
    """Test to validate max character for api port field

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        caseimportance: high
        initialEstimate: 1/15h
    """
    endpoint = RHOSEndpoint(hostname=fauxfactory.gen_alphanumeric(5),
                            api_port=fauxfactory.gen_alphanumeric(16),
                            security_protocol='Non-SSL')
    collection = appliance.collections.cloud_providers
    prov = collection.instantiate(prov_class=OpenStackProvider,
                                  name=fauxfactory.gen_alphanumeric(5),
                                  endpoints=endpoint)
    try:
        prov.create()
    except AssertionError:
        view = prov.create_view(prov.endpoints_form)
        text = view.default.api_port.value
        assert text == prov.default_endpoint.api_port[0:15]


@pytest.mark.tier(2)
@pytest.mark.provider([AzureProvider], scope="function", override=True)
def test_azure_subscription_required(request, provider):
    """
    Tests that provider can't be added w/o subscription

    Metadata:
        test_flag: crud

    Polarion:
        assignee: anikifor
        casecomponent: WebUI
        caseposneg: negative
        initialEstimate: 1/10h
        testSteps:
            1.Add Azure Provider w/0 subscription
            2.Validate
    """
    provider.subscription_id = ''
    request.addfinalizer(provider.delete_if_exists)
    with pytest.raises(AssertionError,
                       match='Credential validation was not successful: Incorrect credentials '
                             '- check your Azure Subscription ID'):
        provider.create()


@pytest.mark.tier(2)
@pytest.mark.usefixtures('has_no_cloud_providers')
def test_azure_multiple_subscription(appliance, request, soft_assert):
    """
    Verifies that different azure providers have different resources access

    Steps:
    1. Add all Azure providers
    2. Compare their VMs/Templates

    Metadata:
        test_flag: crud

    Polarion:
        assignee: anikifor
        casecomponent: Cloud
        initialEstimate: 1/4h
    """
    pf = ProviderFilter(classes=[AzureProvider], required_flags=['crud'])
    providers = list_providers([pf])
    if len(providers) < 2:
        pytest.skip("this test needs at least 2 AzureProviders")
    prov_inventory = []
    for provider in providers:
        request.addfinalizer(provider.clear_providers)
        provider.create()
        provider.validate_stats()
        prov_inventory.append((provider.name,
                               provider.num_vm(),
                               provider.num_template()))

    for index, prov_a in enumerate(prov_inventory[:-1]):
        for prov_b in prov_inventory[index + 1:]:
            soft_assert(prov_a[1] != prov_b[1], "Same num_vms for {} and {}".format(prov_a[0],
                                                                               prov_b[0]))
            soft_assert(prov_a[2] != prov_b[2], "Same num_templates for {} and {}".format(prov_a[0],
                                                                                     prov_b[0]))


@test_requirements.general_ui
@pytest.mark.tier(3)
def test_openstack_provider_has_api_version(appliance):
    """Check whether the Keystone API version field is present for Openstack.

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        initialEstimate: 1/4h
    """
    view = navigate_to(appliance.collections.cloud_providers, 'Add')
    view.fill({"prov_type": "OpenStack"})
    assert view.api_version.is_displayed, "API version select is not visible"


def test_openstack_provider_has_dashboard(appliance, openstack_provider):
    """Check whether dashboard view is available for Openstack provider

    Bugzilla:
        1487142

    Polarion:
        assignee: anikifor
        casecomponent: Cloud
        initialEstimate: 1/12h
        startsin: 5.10
    """
    view = navigate_to(openstack_provider, 'Details', use_resetter=False)
    view.toolbar.view_selector.select('Dashboard View')
    assert view.is_displayed


@pytest.mark.tier(3)
@pytest.mark.provider([EC2Provider], scope="function", override=True)
def test_select_key_pair_none_while_provisioning(appliance, request, has_no_cloud_providers,
                                                 provider):
    """
        GH Issue: https://github.com/ManageIQ/manageiq/issues/10575

        Requirement: Have an ec2 provider with single key pair
                    (For now available in South America (Sao Paulo) region)
        1. Compute -> Cloud -> Instances
        2. Click on Provision Instances in Toolbar
        3. Go to Properties
        4. Select None in Guest Access Key Pair
        5. None should be selected

    Polarion:
        assignee: mmojzis
        casecomponent: WebUI
        initialEstimate: 1/4h
    """
    if 'govcloud' in provider.data.tags:
        pytest.skip("providers with such tag aren't supported for some reason")

    provider.region_name = 'South America (Sao Paulo)'
    request.addfinalizer(provider.delete_if_exists)

    provider.create()
    provider.validate()

    view = navigate_to(appliance.collections.cloud_instances, 'Provision', wait_for_view=0)
    view.image_table[0].click()
    view.form.continue_button.click()

    view.form.properties.guest_keypair.fill('<None>')
    # check drop down was updated with selected value
    assert view.form.properties.guest_keypair.read() == '<None>'


@pytest.mark.tier(3)
@pytest.mark.provider([AzureProvider], override=True)
def test_azure_instance_password_requirements(appliance, request,
        has_no_cloud_providers, setup_provider):
    """
        Requirement: Have an Azure provider
        1. Compute -> Cloud -> Instances
        2. Click on Provision Instances in Toolbar
        3. Select template.
        4. Go to Customisation, fill password that doesn't match the criteria:
            * must be 12-72 characters
            * have 3 of the following - one lowercase character, one uppercase character,
              one number and one special character
        5. Error message should be displayed.

    Polarion:
        assignee: anikifor
        casecomponent: WebUI
        initialEstimate: 1/4h
    """
    view = navigate_to(appliance.collections.cloud_instances, 'Provision')
    view.image_table[0].click()
    view.form.continue_button.click()
    message = (
        "'Customize/Password' must be correctly formatted. The password must be 12-72 characters, "
        "and have 3 of the following - one lowercase character, one uppercase character, "
        "one number and one special character.")

    view.form.customize.fill({
        "admin_username": "some_value",
    })

    for pw in ("abcdefghijkl_",
               "ABCDEFGHIJKL_",
               "ABCDEFGHIJKLa",
               "abcdefgh_1A"):
        view.form.customize.fill({"root_password": pw})
        view.form.submit_button.click()
        wait_for(lambda: message in (m.read() for m in view.flash.messages),
                 fail_condition=False, num_sec=10, delay=.1)
        for m in view.flash.messages:
            m.dismiss()


@pytest.mark.tier(3)
def test_cloud_names_grid_floating_ips(appliance, ec2_provider, soft_assert):
    """
        Requirement: Cloud provider with floating IPs

        Go to Network -> Floating IPs
        Change view to grid
        Test if names are displayed

    Polarion:
        assignee: anikifor
        caseimportance: medium
        casecomponent: WebUI
        initialEstimate: 1/30h
    """
    floating_ips_collection = appliance.collections.network_floating_ips
    view = navigate_to(floating_ips_collection, "All")
    view.toolbar.view_selector.select('Grid View')
    for entity in view.entities.get_all():
        soft_assert('title="{}"'.format(entity.data['address']) in entity.data['quadicon'])


@test_requirements.general_ui
@pytest.mark.tier(3)
def test_display_network_topology(appliance, openstack_provider):
    """
    Bugzilla:
        1343553

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        caseimportance: medium
        initialEstimate: 1/8h
        testSteps:
            1. Add RHOS undercloud provider
            2. Make sure it has no floating IPs
            3. Go to Networks -> Topology
            4. Topology should be shown without errors.

    """
    floating_ips_collection = appliance.collections.network_floating_ips
    view = navigate_to(floating_ips_collection, "All")
    if not view.entities.get_all():
        pytest.skip("No Floating IPs needed for this test")

    topology_col = appliance.collections.network_topology_elements
    view = navigate_to(topology_col, 'All')
    assert view.is_displayed
    view.flash.assert_no_error()


class TestProvidersRESTAPI(object):
    @pytest.fixture(scope="function")
    def arbitration_profiles(self, request, appliance, cloud_provider):
        num_profiles = 2
        response = _arbitration_profiles(
            request, appliance, cloud_provider, num=num_profiles)
        assert_response(appliance)
        assert len(response) == num_profiles

        return response

    @pytest.mark.tier(3)
    @pytest.mark.parametrize('from_detail', [True, False], ids=['from_detail', 'from_collection'])
    def test_cloud_networks_query(self, cloud_provider, appliance, from_detail):
        """Tests querying cloud providers and cloud_networks collection for network info.

        Metadata:
            test_flag: rest

        Polarion:
            assignee: pvala
            casecomponent: Cloud
            caseimportance: low
            initialEstimate: 1/3h
        """
        if from_detail:
            networks = appliance.rest_api.collections.providers.get(
                name=cloud_provider.name).cloud_networks
        else:
            networks = appliance.rest_api.collections.cloud_networks
        assert_response(appliance)
        assert networks
        assert len(networks) == networks.subcount

        enabled_networks = 0
        networks.reload(expand=True)
        for network in networks:
            assert 'CloudNetwork' in network.type
            if network.enabled is True:
                enabled_networks += 1
        assert enabled_networks >= 1

    @pytest.mark.tier(3)
    def test_security_groups_query(self, cloud_provider, appliance):
        """Tests querying cloud networks subcollection for security groups info.

        Metadata:
            test_flag: rest

        Polarion:
            assignee: pvala
            casecomponent: Cloud
            caseimportance: low
            initialEstimate: 1/4h
        """
        network = appliance.rest_api.collections.providers.get(
            name=cloud_provider.name).cloud_networks[0]
        network.reload(attributes='security_groups')
        security_groups = network.security_groups
        # "security_groups" needs to be present, even if it's just an empty list
        assert isinstance(security_groups, list)
        # if it's not empty, check type
        if security_groups:
            assert 'SecurityGroup' in security_groups[0]['type']


@pytest.mark.provider([CloudProvider], override=True, selector=ONE)
def test_tagvis_provision_fields(setup_provider, request, appliance, user_restricted, tag,
                                 soft_assert):
    """Test for network environment fields for restricted user

    Polarion:
        assignee: anikifor
        casecomponent: Tagging
        caseimportance: medium
        initialEstimate: 1/3h
    """
    image = appliance.collections.cloud_images.all()[0]
    image.add_tag(tag)
    request.addfinalizer(lambda: image.remove_tag(tag))
    with user_restricted:
        view = navigate_to(appliance.collections.cloud_instances, 'Provision')
        soft_assert(len(view.image_table.read()) == 1)
        view.image_table.row(name=image.name).click()
        view.form.continue_button.click()
        environment_fields_check = [view.form.environment.cloud_tenant,
                                    view.form.environment.availability_zone,
                                    view.form.environment.cloud_network,
                                    view.form.environment.security_groups,
                                    view.form.environment.public_ip_address,
                                    view.form.properties.guest_keypair]

        soft_assert(len(select) == 1 for select in environment_fields_check)


@test_requirements.general_ui
@pytest.mark.tier(3)
@pytest.mark.provider([OpenStackProvider], override=True)
def test_domain_id_validation(request, provider):
    """ Test validating Keystone V3 needs domain_id

    prerequisites:
        * appliance

    Steps:
        * Navigate add Cloud provider and select OpenStack
        * Select Keystone V3 as API Version
        * Validate without Domain ID

    Polarion:
        assignee: pvala
        casecomponent: WebUI
        initialEstimate: 1/4h
    """
    prov = provider
    prov.api_version = 'Keystone v3'
    prov.keystone_v3_domain_id = None
    request.addfinalizer(prov.delete_if_exists)
    # It must raise an exception because it keeps on the form
    with pytest.raises(AssertionError):
        prov.create()
    view = prov.create_view(CloudProviderAddView)

    # ToDo: Assert proper flash message after BZ-1545520 fix.
    assert view.flash.messages[0].type == 'error'


@pytest.mark.manual
@pytest.mark.tier(1)
def test_sdn_nsg_arrays_refresh_azure():
    """
    Polarion:
        assignee: anikifor
        casecomponent: Cloud
        caseimportance: medium
        initialEstimate: 1/6h
        testSteps:
            1. Add Network Security group on Azure with coma separated port ranges
            `1023,1025` rule inbound/outbound ( ATM this feature is not allowed in
            East US region of Azure - try West/Central)
            2. Add such Azure Region into CFME
            3. Refresh provider
        expectedResults:
            1. The group is successfully added
            2. The region is successfully added
            3. Refreshed succesfully, there are no errors in the logs

    Bugzilla:
        1520196
    """
    pass


@pytest.mark.manual
@pytest.mark.tier(2)
def test_provider_flavors_azure():
    """
    Verify that the vm flavors in Azure are of the correct sizes and that
    the size display in CFME is accurate.
    Low priority as it is unlikely to change once set.  Will want to check
    when azure adds new sizes.  Only need to spot check a few values.
    For current size values, you can check here:
    https://docs.microsoft.com/en-us/azure/virtual-machines/linux/sizes

    Polarion:
        assignee: anikifor
        casecomponent: Cloud
        caseimportance: low
        initialEstimate: 1/8h
        startsin: 5.6
        testSteps:
            1. Add Azure provider
            2. Navigate to Flavours
        expectedResults:
            1. The provider is successfully added
            2. Flavours are the same as in MS documentation
    Bugzilla:
        1357086
    """
    pass


@pytest.mark.manual
@pytest.mark.tier(1)
def test_market_place_images_azure():
    """
    Polarion:
        assignee: anikifor
        casecomponent: Cloud
        caseimportance: medium
        initialEstimate: 1/6h
        testSteps:
            1.Enable market place images
            2.Add Azure provider
            3.Refresh the provider
        expectedResults:
            1.
            2.
            3. Refresh is done fast (faster than 15 minutes)
    Bugzilla:
        1491330
    """
    pass


@pytest.mark.ignore_stream('5.11')
@pytest.mark.tier(1)
def test_create_azure_vm_from_azure_image(connect_az_account, cfme_vhd, upload_image_to_azure,
                                          vm_ip):
    """
    To run this test Azure account is required.

    Azure VM is provisioned from another VM using Powershell, that can be run on any provider.

    Polarion:
        assignee: anikifor
        casecomponent: Cloud
        caseimportance: high
        initialEstimate: 1/2h
        setup: # Virtual Machine Name - as it appears in Azure
               $VMName = "myVmName"
               $ResourceGroupName = "CFMEQE-Main"
               Break
               # Existing Azure Deployment Values - Video with instructions
               forthcoming.
               $AvailabilitySetName = "cfmeqe-as-free"
               $AzureLocation = "East US"
               $VMDeploymentSize= "Standard_A1"
               $StorageAccountName = "cfmeqestore"
               $BlobContainerName = "templates"
               $VHDName = "cfme-azure-56013.vhd"
               $VirtualNetworkName = "cfmeqe"
               $NetworkSecurityGroupName = "cfmeqe-nsg"
               $VirtualNetworkSubnetName = "default"
               $VirtualNetworkAddressPrefix = "10.0.0.0/16"
               $VirtualNetworkSubnetAddressPrefix = "10.0.0.0/24"
               # Create VM Components
               $StorageAccount = Get-AzureRmStorageAccount -ResourceGroupName
               $ResourceGroupName -Name $StorageAccountName
               $InterfaceName = $VMName
               $NetworkSecurityGroupID = Get-AzureRmNetworkSecurityGroup -Name
               $NetworkSecurityGroupName -ResourceGroupName $ResourceGroupName
               $PIp = New-AzureRmPublicIpAddress -Name $InterfaceName
               -ResourceGroupName $ResourceGroupName -Location $AzureLocation
               -AllocationMethod Dynamic -Force
               $SubnetConfig = New-AzureRmVirtualNetworkSubnetConfig -Name
               $VirtualNetworkSubnetName -AddressPrefix
               $VirtualNetworkSubnetAddressPrefix
               $VNet = New-AzureRmVirtualNetwork -Name $VirtualNetworkName
               -ResourceGroupName $ResourceGroupName -Location $AzureLocation
               -AddressPrefix $VirtualNetworkAddressPrefix -Subnet $SubnetConfig
               -Force
               $Interface = New-AzureRmNetworkInterface -Name $InterfaceName
               -ResourceGroupName $ResourceGroupName -Location $AzureLocation
               -SubnetId $VNet.Subnets[0].Id -PublicIpAddressId $PIp.Id -Force
               $AvailabilitySet = Get-AzureRmAvailabilitySet -ResourceGroupName
               $ResourceGroupName -Name $AvailabilitySetName
               $VirtualMachine = New-AzureRmVMConfig -VMName $VMName -VMSize
               $VMDeploymentSize -AvailabilitySetID $AvailabilitySet.Id
               $VirtualMachine = Add-AzureRmVMNetworkInterface -VM $VirtualMachine
               -Id $Interface.Id
               $OSDiskUri = $StorageAccount.PrimaryEndpoints.Blob.ToString() +
               $BlobContainerName + "/" + $VHDName
               $VirtualMachine = Set-AzureRmVMOSDisk -VM $VirtualMachine -Name
               $VMName -VhdUri $OSDiskUri -CreateOption attach -Linux
               # Create the Virtual Machine
               New-AzureRmVM -ResourceGroupName $ResourceGroupName -Location
               $AzureLocation -VM $VirtualMachine
        testSteps:
            1. Make the VM
            2. Config SSH support
            3. Config DNS is desired.
            4. SSH into new VM with Azure Public IP address and verify it has booted
            correctly.
            5. Use HTTP to DNS into the appliance web ui and make sure
            you can log in.
        startsin: 5.6
        teardown: When you"re done, delete everything.  Make sure at a minimum that the
                  VM is completely Stopped in Azure.
        title: Create Azure VM from Azure image
    """
    app = appliance.IPAppliance.from_url(vm_ip)

    # Credentials for the provisioned VM from CFME image, this is different to the VM that runs
    # powershell scripts as Azure has specific requirements for login/password.
    # These credentials are used in the script create_vm.ps1 to provision the VM.
    username = credentials['azure_appliance']['username']
    password = credentials['azure_appliance']['password']

    with ssh.SSHClient(hostname=vm_ip,
                       username=username,
                       password=password) as app_ssh_client:

        # permit root login over ssh for future appliance configuration
        command = 'sed -i "s/.*PermitRootLogin.*/PermitRootLogin yes/g" /etc/ssh/sshd_config'
        config = app_ssh_client.run_command(
            'echo {} | sudo -S {}'.format(password, command), ensure_user=True)
        assert config.success

        # restart sshd to apply configuration changes
        restart = app_ssh_client.run_command(
            'echo {} | sudo -S systemctl restart sshd'.format(password), ensure_user=True)
        assert restart.success

        # unlock root password
        unlock = app_ssh_client.run_command(
            'echo {} | sudo -S passwd -u root'.format(password), ensure_user=True)
        assert unlock.success

    app.configure()
    app.wait_for_web_ui()

    # Check we can login
    logged_in_page = app.server.login()
    assert logged_in_page.is_displayed


@pytest.mark.manual
def test_ec2_refresh_with_stack_without_parameters():
    """
    1) Add cloudformation stack without parameters(https://s3-us-
    west-2.amazonaws.com/cloudformation-templates-us-
    west-2/Managed_EC2_Batch_Environment.template  )
    2) Add ec2 provider with cloudformation stack without parameters
    3) Wait for refresh - it should be refreshed successfully without
    errors

    Polarion:
        assignee: mmojzis
        casecomponent: Cloud
        initialEstimate: 1/5h
    """
    pass


@pytest.mark.manual
def test_ec2_public_images():
    """
    1) Set
    :ems_refresh:
    :ec2:
    :get_public_images: true
    2) Add an ec2 provider
    3) Wait for its refresh(It can take more than 30 minutes)
    4) Refresh should be successful and there should be more than 100k ec2
    images

    Polarion:
        assignee: mmojzis
        caseimportance: critical
        initialEstimate: 2/3h
        casecomponent: Cloud
    """
    pass


@pytest.mark.manual
def test_ec2_api_filter_limit():
    """
    Bugzilla:
        1612086

    The easiest way to simulate AWS API Limit for > 200 items is to enable
    and disable public images:
    Requirement: Have an ec2 provider
    1) Enable public images for ec2 in Advanced Settings
    2) Wait for public images to be refreshed
    3) Disable public images for ec2 in Advanced Settings
    4) Wait for public images to be refreshed (cleared)

    Polarion:
        assignee: mmojzis
        casecomponent: Cloud
        initialEstimate: 1 1/3h
        startsin: 5.9
    """
    pass


@pytest.mark.manual
def test_ec2_create_sns_topic():
    """
    Requires: No SNS topic for tested region
    1) Add an ec2 provider with tested region
    2) Wait 3 minutes
    3) Check SNS topic for this region in AWS Console

    Polarion:
        assignee: mmojzis
        casecomponent: Cloud
        initialEstimate: 1/6h
        startsin: 5.8
    """
    pass


@pytest.mark.manual
def test_ec2_add_delete_add_provider():
    """
    Polarion:
        assignee: mmojzis
        casecomponent: Cloud
        initialEstimate: 1h
    """
    pass


@pytest.mark.manual
def test_ec2_deploy_instance_with_ssh_addition_template():
    """
    Requirement: EC2 provider
    1) Provision an instance
    2) Select Choose Automatically in Environment -> Placement
    3) Select SSH key addition template in Customize -> Customize Template
    4) Instance should be provisioned without any errors

    Polarion:
        assignee: mmojzis
        casecomponent: Cloud
        caseimportance: medium
        initialEstimate: 1/6h
    """
    pass


@pytest.mark.manual
def test_add_ec2_provider_with_instance_without_name():
    """
    1) Add an ec2 provider with instance without name
    2) Wait for refresh
    3) Refresh should complete without errors

    Polarion:
        assignee: mmojzis
        casecomponent: Cloud
        initialEstimate: 1/6h
    """
    pass

