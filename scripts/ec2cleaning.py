from datetime import datetime
from utils.providers import list_providers, get_mgmt
from time import sleep
date = datetime.now()
deletetime = 60 * 60 * 24 * 7  # 7 Days

def delete_old_instances(ec2provider, date, deletetime):
    for instance in ec2provider.list_vm(include_terminated=True):
        creation = ec2provider.vm_creation_time(instance)
        difference = (date - creation).total_seconds()
        if (difference >= deletetime):
            print instance

def delete_disassociated_addresses(ec2provider):
    for ip in ec2provider._get_all_disassociated_addresses():
        if ip.allocation_id:
            ec2provider._release_vpc_address(alloc_id=ip.allocation_id)
        else:
            ec2provider._release_address(address=ip.public_ip)
        print ip.public_ip or ip.allocation_id
#
def delete_unattached_volumes(ec2provider):
    for volume in ec2provider._get_all_unattached_volumes():
        volume.delete()
        print volume

for provider in list_providers('ec2'):
    ec2provider = get_mgmt(provider)
    print (provider + ":\n")
    print ("Deleted instances:")
    delete_old_instances(ec2provider=ec2provider,date=date,deletetime=deletetime)
    sleep(300)
    print ("Released addresses:")
    delete_disassociated_addresses(ec2provider)
    print ("Deleted volumes:")
    delete_unattached_volumes(ec2provider)