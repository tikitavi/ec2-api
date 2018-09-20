# Copyright 2014
# The Cloudscaling Group, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

try:
    from neutronclient.common import exceptions as neutron_exception
except ImportError:
    pass  # clients will log absense of neutronclient in this case
from oslo_config import cfg
from oslo_log import log as logging

from ec2api.api import common
from ec2api.api import ec2utils
from ec2api.api import internet_gateway as internet_gateway_api
from ec2api import clients
from ec2api.db import api as db_api
from ec2api import exception
from ec2api.i18n import _

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

"""Address related API implementation
"""


Validator = common.Validator


def get_address_engine():
    return AddressEngineNeutron()


def allocate_address(context, domain=None):
    if domain and domain not in ['vpc', 'standard']:
        msg = _("Invalid value '%(domain)s' for domain.") % {'domain': domain}
        raise exception.InvalidParameterValue(msg)

    address, os_floating_ip = address_engine.allocate_address(context, domain)
    return _format_address(context, address, os_floating_ip)


def associate_address(context, public_ip=None, instance_id=None,
                      allocation_id=None, network_interface_id=None,
                      private_ip_address=None, allow_reassociation=False):
    if not public_ip and not allocation_id:
        msg = _('Either public IP or allocation id must be specified')
        raise exception.MissingParameter(msg)
    if public_ip and allocation_id:
        msg = _('You may specify public IP or allocation id, '
                'but not both in the same call')
        raise exception.InvalidParameterCombination(msg)
    if not instance_id and not network_interface_id:
        msg = _('Either instance ID or network interface id must be specified')
        raise exception.MissingParameter(msg)
    associationId = address_engine.associate_address(
        context, public_ip, instance_id,
        allocation_id, network_interface_id,
        private_ip_address, allow_reassociation)
    if associationId:
        LOG.info('Associated with id %s', associationId)
        return {'return': True,
                'associationId': associationId}
    return {'return': True}


def disassociate_address(context, public_ip=None, association_id=None):
    LOG.info('Disassociate address: %(public_ip)s. Association: %(association_id)s.',
                    {'public_ip': public_ip, 'association_id': association_id})
    if not public_ip and not association_id:
        msg = _('Either public IP or association id must be specified')
        raise exception.MissingParameter(msg)
    if public_ip and association_id:
        msg = _('You may specify public IP or association id, '
                'but not both in the same call')
        raise exception.InvalidParameterCombination(msg)
    disassociate = address_engine.disassociate_address(context, public_ip, association_id)
    LOG.info('Disassociation is %s', disassociate)
    return True


def release_address(context, public_ip=None, allocation_id=None):
    if not public_ip and not allocation_id:
        msg = _('Either public IP or allocation id must be specified')
        raise exception.MissingParameter(msg)
    if public_ip and allocation_id:
        msg = _('You may specify public IP or allocation id, '
                'but not both in the same call')
        raise exception.InvalidParameterCombination(msg)

    address_engine.release_address(context, public_ip, allocation_id)
    return True


class AddressDescriber(common.UniversalDescriber):

    KIND = 'eipalloc'
    FILTER_MAP = {'allocation-id': 'allocationId',
                  'association-id': 'associationId',
                  'domain': 'domain',
                  'instance-id': 'instanceId',
                  'network-interface-id': 'networkInterfaceId',
                  'network-interface-owner-id': 'networkInterfaceOwnerId',
                  'private-ip-address': 'privateIpAddress',
                  'public-ip': 'publicIp'}

    def __init__(self, os_ports, db_instances):
        self.os_ports = os_ports
        self.db_instances_dict = {i['os_id']: i for i in (db_instances or [])}

    def format(self, item=None, os_item=None):
        return _format_address(self.context, item, os_item, self.os_ports,
                               self.db_instances_dict)

    def get_os_items(self):
        return address_engine.get_os_floating_ips(self.context)

    def auto_update_db(self, item, os_item):
        item = super(AddressDescriber, self).auto_update_db(item, os_item)
        if (item and 'network_interface_id' in item and
                (not os_item.get('port_id') or
                 os_item['fixed_ip_address'] != item['private_ip_address'])):
            _disassociate_address_item(self.context, item)
        return item

    def get_name(self, os_item):
        return os_item['floating_ip_address']


def describe_addresses(context, public_ip=None, allocation_id=None,
                       filter=None):
    LOG.info('Describe_addresses with allocation_id %s', allocation_id)
    formatted_addresses = AddressDescriber(
        address_engine.get_os_ports(context),
        db_api.get_items(context, 'i')).describe(
            context, allocation_id, public_ip, filter)
    LOG.info('Formatted addresses: %s', formatted_addresses)
    return {'addressesSet': formatted_addresses}


def _format_address(context, address, os_floating_ip, os_ports=[],
                    db_instances_dict=None):
    ec2_address = {'publicIp': os_floating_ip['floating_ip_address']}
    fixed_ip_address = os_floating_ip.get('fixed_ip_address')
    if fixed_ip_address:
        ec2_address['privateIpAddress'] = fixed_ip_address
        os_instance_id = _get_os_instance_id(context, os_floating_ip, os_ports)
        if os_instance_id:
            ec2_address['instanceId'] = (
                _get_instance_ec2_id_by_os_id(context, os_instance_id,
                                              db_instances_dict))
    if not address:
        ec2_address['domain'] = 'standard'
    else:
        ec2_address['domain'] = 'vpc'
        ec2_address['allocationId'] = address['id']
        if 'network_interface_id' in address:
            ec2_address.update({
                    'associationId': ec2utils.change_ec2_id_kind(
                            ec2_address['allocationId'], 'eipassoc'),
                    'networkInterfaceId': address['network_interface_id'],
                    'networkInterfaceOwnerId': context.project_id})
    return ec2_address


def _get_instance_ec2_id_by_os_id(context, os_instance_id, db_instances_dict):
    db_item = ec2utils.get_db_item_by_os_id(context, 'i', os_instance_id,
                                            db_instances_dict)
    return db_item['id']


def _is_address_valid(context, neutron, address):
    try:
        neutron.show_floatingip(address['os_id'])
    except neutron_exception.NotFound:
        return False
    else:
        return True


def _associate_address_item(context, address, network_interface_id,
                            private_ip_address):
    address['network_interface_id'] = network_interface_id
    address['private_ip_address'] = private_ip_address
    db_api.update_item(context, address)


def _disassociate_address_item(context, address):
    address.pop('network_interface_id')
    address.pop('private_ip_address')
    db_api.update_item(context, address)


def _get_os_instance_id(context, os_floating_ip, os_ports=[]):
    port_id = os_floating_ip.get('port_id')
    os_instance_id = None
    if port_id:
        port = next((port for port in os_ports
                     if port['id'] == port_id), None)
        if port:
            os_instance_id = port.get('device_id')
    return os_instance_id


class AddressEngineNeutron(object):

    def allocate_address(self, context, domain=None):
        os_public_network = ec2utils.get_os_public_network(context)
        neutron = clients.neutron(context)

        with common.OnCrashCleaner() as cleaner:
            os_floating_ip = {'floating_network_id': os_public_network['id']}
            try:
                os_floating_ip = neutron.create_floatingip(
                        {'floatingip': os_floating_ip})
            except neutron_exception.OverQuotaClient:
                raise exception.AddressLimitExceeded()
            os_floating_ip = os_floating_ip['floatingip']
            if ((not domain or domain == 'standard') and
                 not CONF.disable_ec2_classic):
                return None, os_floating_ip
            cleaner.addCleanup(neutron.delete_floatingip, os_floating_ip['id'])

            address = {'os_id': os_floating_ip['id'],
                       'public_ip': os_floating_ip['floating_ip_address']}
            address = db_api.add_item(context, 'eipalloc', address)
        return address, os_floating_ip

    def release_address(self, context, public_ip, allocation_id):
        neutron = clients.neutron(context)
        if public_ip:
            # TODO(ft): implement search in DB layer
            address = next((addr for addr in
                            db_api.get_items(context, 'eipalloc')
                            if addr['public_ip'] == public_ip), None)
            if address and _is_address_valid(context, neutron, address):
                msg = _('You must specify an allocation id when releasing a '
                        'VPC elastic IP address')
                raise exception.InvalidParameterValue(msg)
            os_floating_ip = self.get_os_floating_ip_by_public_ip(context,
                                                                  public_ip)
            try:
                neutron.delete_floatingip(os_floating_ip['id'])
            except neutron_exception.NotFound:
                pass
            return

        address = ec2utils.get_db_item(context, allocation_id)
        if not _is_address_valid(context, neutron, address):
            raise exception.InvalidAllocationIDNotFound(
                id=allocation_id)

        if 'network_interface_id' in address:
            if CONF.disable_ec2_classic:
                network_interface_id = address['network_interface_id']
                network_interface = db_api.get_item_by_id(context,
                                                          network_interface_id)
                default_vpc = ec2utils.check_and_create_default_vpc(context)
                if default_vpc:
                    default_vpc_id = default_vpc['id']
                if (network_interface and
                        network_interface['vpc_id'] == default_vpc_id):
                    association_id = ec2utils.change_ec2_id_kind(address['id'],
                                                                 'eipassoc')
                    self.disassociate_address(
                        context, association_id=association_id)
                else:
                    raise exception.InvalidIPAddressInUse(
                        ip_address=address['public_ip'])
            else:
                raise exception.InvalidIPAddressInUse(
                    ip_address=address['public_ip'])

        with common.OnCrashCleaner() as cleaner:
            db_api.delete_item(context, address['id'])
            cleaner.addCleanup(db_api.restore_item, context,
                               'eipalloc', address)
            try:
                neutron.delete_floatingip(address['os_id'])
            except neutron_exception.NotFound:
                pass

    def associate_address(self, context, public_ip=None, instance_id=None,
                          allocation_id=None, network_interface_id=None,
                          private_ip_address=None, allow_reassociation=False):
        instance_network_interfaces = []
        if instance_id:
            # TODO(ft): implement search in DB layer
            for eni in db_api.get_items(context, 'eni'):
                if eni.get('instance_id') == instance_id:
                    instance_network_interfaces.append(eni)

        neutron = clients.neutron(context)

        if public_ip:
            # TODO(ft): implement search in DB layer
            address = next((addr for addr in db_api.get_items(context,
                                                              'eipalloc')
                            if addr['public_ip'] == public_ip), None)

            if not CONF.disable_ec2_classic:
                if instance_network_interfaces:
                    msg = _('You must specify an allocation id when mapping '
                            'an address to a VPC instance')
                    raise exception.InvalidParameterCombination(msg)
                if address and _is_address_valid(context, neutron, address):
                    msg = _(
                        "The address '%(public_ip)s' does not belong to you.")
                    raise exception.AuthFailure(msg % {'public_ip': public_ip})

                os_instance_id = ec2utils.get_db_item(context,
                                                      instance_id)['os_id']
                # NOTE(ft): check the public IP exists to raise AWS exception
                # otherwise
                self.get_os_floating_ip_by_public_ip(context, public_ip)
                nova = clients.nova(context)
                nova.servers.add_floating_ip(os_instance_id, public_ip)
                return None

            if not address:
                msg = _("The address '%(public_ip)s' does not belong to you.")
                raise exception.AuthFailure(msg % {'public_ip': public_ip})
            allocation_id = address['id']

        if instance_id:
            if not instance_network_interfaces:
                # NOTE(ft): check the instance exists
                ec2utils.get_db_item(context, instance_id)
                msg = _('You must specify an IP address when mapping '
                        'to a non-VPC instance')
                raise exception.InvalidParameterCombination(msg)
            if len(instance_network_interfaces) > 1:
                raise exception.InvalidInstanceId(instance_id=instance_id)
            network_interface = instance_network_interfaces[0]
        else:
            network_interface = ec2utils.get_db_item(context,
                                                     network_interface_id)
        if not private_ip_address:
            private_ip_address = network_interface['private_ip_address']

        address = ec2utils.get_db_item(context, allocation_id)
        if not _is_address_valid(context, neutron, address):
            raise exception.InvalidAllocationIDNotFound(
                id=allocation_id)

        if address.get('network_interface_id') == network_interface['id']:
            # NOTE(ft): idempotent call
            pass
        elif address.get('network_interface_id') and not allow_reassociation:
            msg = _('resource %(eipalloc_id)s is already associated with '
                    'associate-id %(eipassoc_id)s')
            msg = msg % {'eipalloc_id': allocation_id,
                         'eipassoc_id': ec2utils.change_ec2_id_kind(
                                                address['id'], 'eipassoc')}
            raise exception.ResourceAlreadyAssociated(msg)
        else:
            internet_gateways = (
                internet_gateway_api.describe_internet_gateways(
                    context,
                    filter=[{'name': 'attachment.vpc-id',
                             'value': [network_interface['vpc_id']]}])
                ['internetGatewaySet'])
            if len(internet_gateways) == 0:
                msg = _('Network %(vpc_id)s is not attached to any internet '
                        'gateway') % {'vpc_id': network_interface['vpc_id']}
                raise exception.GatewayNotAttached(msg)

            with common.OnCrashCleaner() as cleaner:
                _associate_address_item(context, address,
                                        network_interface['id'],
                                        private_ip_address)
                cleaner.addCleanup(_disassociate_address_item, context,
                                   address)

                os_floating_ip = {'port_id': network_interface['os_id'],
                                  'fixed_ip_address': private_ip_address}
                neutron.update_floatingip(address['os_id'],
                                          {'floatingip': os_floating_ip})
        # TODO(ft): generate unique association id for each act of association
        return ec2utils.change_ec2_id_kind(address['id'], 'eipassoc')

    def disassociate_address(self, context, public_ip=None,
                             association_id=None):
        LOG.info('Disassociating %s', association_id)
        neutron = clients.neutron(context)

        floatingips=neutron.list_floatingips(tenant_id=context.project_id)['floatingips']
        LOG.info('Existing floating ips: %s', floatingips)

        if public_ip:
            # TODO(ft): implement search in DB layer
            address = next((addr for addr in db_api.get_items(context,
                                                              'eipalloc')
                            if addr['public_ip'] == public_ip), None)

            if not CONF.disable_ec2_classic:
                if address and _is_address_valid(context, neutron, address):
                    msg = _('You must specify an association id when '
                            'unmapping an address from a VPC instance')
                    raise exception.InvalidParameterValue(msg)
                # NOTE(tikitavi): check the public IP exists to raise AWS
                # exception otherwise
                os_floating_ip = self.get_os_floating_ip_by_public_ip(
                    context, public_ip)
                os_ports = self.get_os_ports(context)
                os_instance_id = _get_os_instance_id(context, os_floating_ip,
                                                     os_ports)
                if os_instance_id:
                    nova = clients.nova(context)
                    nova.servers.remove_floating_ip(os_instance_id, public_ip)
                return None

            if not address:
                msg = _("The address '%(public_ip)s' does not belong to you.")
                raise exception.AuthFailure(msg % {'public_ip': public_ip})
            if 'network_interface_id' not in address:
                msg = _('You must specify an association id when unmapping '
                        'an address from a VPC instance')
                raise exception.InvalidParameterValue(msg)
            association_id = ec2utils.change_ec2_id_kind(address['id'],
                                                         'eipassoc')

        address = db_api.get_item_by_id(
            context, ec2utils.change_ec2_id_kind(association_id, 'eipalloc'))
        LOG.info('DB address: %s', address)
        if address is None or not _is_address_valid(context, neutron, address):
            raise exception.InvalidAssociationIDNotFound(
                    id=association_id)
        if 'network_interface_id' in address:
            with common.OnCrashCleaner() as cleaner:
                network_interface_id = address['network_interface_id']
                private_ip_address = address['private_ip_address']
                LOG.info('Disassociating %(private_ip_address)s from interface %(network_interface_id)s',
                         {'private_ip_address': private_ip_address, 'network_interface_id': network_interface_id})
                _disassociate_address_item(context, address)
                cleaner.addCleanup(_associate_address_item, context, address,
                                   network_interface_id, private_ip_address)

                update = neutron.update_floatingip(address['os_id'],
                                                   {'floatingip': {'port_id': None}})
                LOG.info('Neutron.update result is %s', update)

    def get_os_floating_ips(self, context):
        neutron = clients.neutron(context)
        return neutron.list_floatingips(
            tenant_id=context.project_id)['floatingips']

    def get_os_ports(self, context):
        neutron = clients.neutron(context)
        return neutron.list_ports(tenant_id=context.project_id)['ports']

    def get_os_floating_ip_by_public_ip(self, context, public_ip):
        os_floating_ip = next((addr for addr in
                               self.get_os_floating_ips(context)
                               if addr['floating_ip_address'] == public_ip),
                              None)
        if not os_floating_ip:
            msg = _("The address '%(public_ip)s' does not belong to you.")
            raise exception.AuthFailure(msg % {'public_ip': public_ip})
        return os_floating_ip


address_engine = get_address_engine()
