import fauxfactory
import pytest
from Crypto.PublicKey import RSA

from cfme import test_requirements
from cfme.cloud.keypairs import KeyPairAllView
from cfme.cloud.provider.ec2 import EC2Provider
from cfme.cloud.provider.openstack import OpenStackProvider
from cfme.utils.appliance.implementations.ui import navigate_to
from cfme.utils.blockers import BZ

pytestmark = [
    pytest.mark.usefixtures('setup_provider'),
    pytest.mark.provider([EC2Provider, OpenStackProvider], scope="module")
]


@pytest.fixture()
def keypair(appliance, provider):
    key = appliance.collections.cloud_keypairs.create(
        name=fauxfactory.gen_alphanumeric(),
        provider=provider
    )
    assert key.exists
    yield key


@pytest.mark.meta(blockers=[BZ(1718833, forced_streams=["5.10", "5.11"],
                               unblock=lambda provider: provider.one_of(OpenStackProvider))])
@pytest.mark.tier(3)
def test_keypair_crud(appliance, provider):
    """ This will test whether it will create new Keypair and then deletes it.
    Steps:
        * Provide Keypair name.
        * Select Cloud Provider.
        * Also delete it.

    Polarion:
        assignee: mnadeem
        casecomponent: Cloud
        initialEstimate: 1/4h
    """
    keypair = appliance.collections.cloud_keypairs.create(
        name=fauxfactory.gen_alphanumeric(),
        provider=provider
    )
    assert keypair.exists

    keypair.delete(wait=True)
    assert not keypair.exists


@pytest.mark.tier(3)
def test_keypair_crud_with_key(provider, appliance):
    """ This will test whether it will create new Keypair and then deletes it.

    Steps:
        * Provide Keypair name.
        * Select Cloud Provider.
        * Also delete it.

    Polarion:
        assignee: mnadeem
        casecomponent: Cloud
        initialEstimate: 1/4h
    """
    key = RSA.generate(1024)
    public_key = key.publickey().exportKey('OpenSSH')
    keypair = appliance.collections.cloud_keypairs.create(
        fauxfactory.gen_alphanumeric(),
        provider,
        public_key
    )
    assert keypair.exists

    keypair.delete(wait=True)
    assert not keypair.exists


@pytest.mark.tier(3)
def test_keypair_create_cancel(provider, appliance):
    """ This will test cancelling on adding a keypair

    Steps:
        * Provide Keypair name.
        * Select Cloud Provider.
        * Also delete it.

    Polarion:
        assignee: mnadeem
        casecomponent: WebUI
        initialEstimate: 1/4h
    """
    keypair = appliance.collections.cloud_keypairs.create(
        name="",
        provider=provider,
        cancel=True
    )
    view = keypair.create_view(KeyPairAllView)
    assert view.flash.assert_message('Add of new Key Pair was cancelled by the user')


@pytest.mark.uncollectif(lambda provider: provider.one_of(OpenStackProvider))
@pytest.mark.tier(3)
def test_keypair_create_validation(provider, appliance):
    """ This will test validating the new of name of the key pair

    Steps:
        * Try to add key pair with empty name.

    Polarion:
        assignee: mnadeem
        casecomponent: WebUI
        initialEstimate: 1/4h
    """
    keypair_collection = appliance.collections.cloud_keypairs
    view = navigate_to(keypair_collection, 'Add')
    view.fill({'provider': provider})
    assert not view.form.add.active


@test_requirements.tag
@pytest.mark.uncollectif(lambda provider: provider.one_of(EC2Provider))
def test_keypair_add_and_remove_tag(keypair):
    """ This will test whether it will add and remove tag for newly created Keypair or not
    and then deletes it.

    Steps:
        * Provide Keypair name.
        * Select Cloud Provider.
        * Add tag to Keypair.
        * Remove tag from Keypair
        * Also delete it.

    Polarion:
        assignee: anikifor
        casecomponent: Tagging
        initialEstimate: 1/4h
    """
    added_tag = keypair.add_tag()
    tagged_value = keypair.get_tags()
    assert (
        tag.category.display_name == added_tag.category.display_name and
        tag.display_name == added_tag.display_name
        for tag in keypair.get_tags()), (
        'Assigned tag was not found on the details page')

    keypair.remove_tag(added_tag)
    tagged_value1 = keypair.get_tags()
    assert tagged_value1 != tagged_value, "Remove tag failed."
    # Above small conversion in assert statement convert 'tagged_value' in tuple("a","b") and then
    # compare with tag which is tuple. As get_tags will return assigned tag in list format["a: b"].

    keypair.delete(wait=True)

    assert not keypair.exists


@pytest.mark.rfe
def test_download_private_key(keypair):
    """
    Polarion:
        assignee: mnadeem
        casecomponent: Cloud
        initialEstimate: 1/4h
    """
    keypair.download_private_key()
