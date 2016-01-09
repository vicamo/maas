# Copyright 2014-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

""":class:`StaticIPAddress` tests."""

__all__ = []


from random import randint
from unittest import skip

from django.core.exceptions import ValidationError
from django.db import transaction
from maasserver import locks
from maasserver.enum import (
    INTERFACE_LINK_TYPE,
    INTERFACE_TYPE,
    IPADDRESS_FAMILY,
    IPADDRESS_TYPE,
    NODEGROUP_STATUS,
    NODEGROUPINTERFACE_MANAGEMENT,
)
from maasserver.exceptions import (
    StaticIPAddressExhaustion,
    StaticIPAddressOutOfRange,
    StaticIPAddressUnavailable,
)
from maasserver.models import interface as interface_module
from maasserver.models.domain import Domain
from maasserver.models.interface import UnknownInterface
from maasserver.models.staticipaddress import StaticIPAddress
from maasserver.models.vlan import VLAN
from maasserver.testing.factory import factory
from maasserver.testing.orm import reload_object
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import (
    is_serialization_failure,
    transactional,
)
from maastesting.matchers import (
    MockCalledOnceWith,
    MockNotCalled,
)
from mock import sentinel
from netaddr import (
    IPAddress,
    IPRange,
)
from testtools.matchers import (
    Contains,
    Equals,
    HasLength,
    Not,
)


class TestStaticIPAddressManager(MAASServerTestCase):

    def test__clean_discovered_ip_addresses_on_interface_one_interface(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        ipaddr = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, interface=interface)
        StaticIPAddress.objects._clean_discovered_ip_addresses_on_interface(
            interface, ipaddr.subnet.get_ipnetwork().version)
        self.assertIsNone(reload_object(ipaddr))

    def test__clean_discovered_ip_addresses_on_interface_dont_delete(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        ipaddr = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, interface=interface)
        StaticIPAddress.objects._clean_discovered_ip_addresses_on_interface(
            interface, ipaddr.subnet.get_ipnetwork().version,
            dont_delete=[ipaddr])
        self.assertIsNotNone(reload_object(ipaddr))

    def test__clean_discovered_ip_addresses_on_interface_subnet_family(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network_v4 = factory.make_ipv4_network()
        subnet_v4 = factory.make_Subnet(cidr=str(network_v4.cidr))
        ip_v4 = factory.pick_ip_in_network(network_v4)
        ip_v4 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, ip=ip_v4,
            subnet=subnet_v4, interface=interface)
        network_v6 = factory.make_ipv6_network()
        subnet_v6 = factory.make_Subnet(cidr=str(network_v6.cidr))
        ip_v6 = factory.pick_ip_in_network(network_v6)
        ip_v6 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, ip=ip_v6,
            subnet=subnet_v6, interface=interface)
        StaticIPAddress.objects._clean_discovered_ip_addresses_on_interface(
            interface, network_v4.version)
        self.assertIsNone(reload_object(ip_v4))
        self.assertIsNotNone(reload_object(ip_v6))

    def test__clean_discovered_ip_addresses_on_interface_multi_interface(self):
        nic0 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        nic1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        ipaddr = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, interface=nic0)
        nic1.ip_addresses.add(ipaddr)
        StaticIPAddress.objects._clean_discovered_ip_addresses_on_interface(
            nic0, ipaddr.subnet.get_ipnetwork().version)
        ipaddr = reload_object(ipaddr)
        self.assertIsNotNone(ipaddr)
        self.assertItemsEqual(
            [nic1.id], ipaddr.interface_set.values_list("id", flat=True))

    def test_update_leases_new_ip_new_mac(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()

        discovered_ip = str(IPAddress(ngi.get_dynamic_ip_range()[0]))
        discovered_mac = factory.make_mac_address()

        StaticIPAddress.objects.update_leases(
            node.nodegroup, [(discovered_ip, discovered_mac)])
        new_ips = StaticIPAddress.objects.filter(ip=discovered_ip)
        self.assertEqual(1, len(new_ips))
        self.assertEqual(
            IPADDRESS_TYPE.DISCOVERED, new_ips[0].alloc_type)
        self.assertEqual(
            INTERFACE_TYPE.UNKNOWN, new_ips[0].interface_set.first().type)

    def test_update_leases_clears_lease(self):
        vlan = VLAN.objects.get_default_vlan()
        node = factory.make_Node_with_Interface_on_Subnet(vlan=vlan)
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()

        # Create DISCOVERED IP address that will be cleared.
        discovered_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            ip=str(IPAddress(ngi.get_dynamic_ip_range()[0])),
            subnet=subnet, interface=boot_interface)

        StaticIPAddress.objects.update_leases(node.nodegroup, [])
        discovered_ip = reload_object(discovered_ip)
        self.assertIsNone(discovered_ip.ip)
        self.assertEqual(subnet, discovered_ip.subnet)

    def test_update_leases_adds_new_lease_keeps_old_subnet_link(self):
        vlan = VLAN.objects.get_default_vlan()
        node = factory.make_Node_with_Interface_on_Subnet(vlan=vlan)
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()

        # Create DISCOVERED IP address that will be cleared.
        discovered_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            ip=str(IPAddress(ngi.get_dynamic_ip_range()[0])),
            subnet=subnet, interface=boot_interface)

        # Create another interface that is linked to the same IP address as
        # the interface for this test.
        other_interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        other_interface.ip_addresses.add(discovered_ip)

        # Update the leases.
        leases = [(discovered_ip.ip, str(boot_interface.mac_address))]
        StaticIPAddress.objects.update_leases(node.nodegroup, leases)

        # New discovered IP address.
        boot_interface = reload_object(boot_interface)
        lease_ip = boot_interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.DISCOVERED).first()
        self.assertEqual(discovered_ip.ip, lease_ip.ip)
        self.assertEqual(subnet, lease_ip.subnet)

        # Old empty discovered IP address.
        other_interface = reload_object(other_interface)
        extra_ip = other_interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.DISCOVERED).first()
        self.assertIsNone(extra_ip.ip)
        self.assertEqual(subnet, lease_ip.subnet)

    @skip("XXX LaMontJones 2016-01-08 fails sporadically.  Bug#1532359")
    def test_update_leases_handles_multiple_empty_ips(self):
        cidr = str(factory.make_ipv4_network().cidr)
        node = factory.make_Node_with_Interface_on_Subnet(cidr=cidr)
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()
        # Create a pre-1.9 condition in the StaticIPAddress table.
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, ip=None, subnet=subnet)
        discovered_ip = str(IPAddress(ngi.get_dynamic_ip_range()[0]))
        macs = [factory.make_mac_address() for i in range(2)]
        StaticIPAddress.objects.update_leases(
            node.nodegroup, [(discovered_ip, macs[0])])
        # Now move to the new MAC, and ensure that the table is correctly
        # updated, even when multiple empty IP addresses are in the table.
        # (See also bug #1513485).
        StaticIPAddress.objects.update_leases(
            node.nodegroup, [(discovered_ip, macs[1])])
        new_ips = StaticIPAddress.objects.filter(ip=discovered_ip)
        self.assertEqual(1, len(new_ips))
        self.assertEqual(
            IPADDRESS_TYPE.DISCOVERED, new_ips[0].alloc_type)
        self.assertEqual(
            INTERFACE_TYPE.UNKNOWN, new_ips[0].interface_set.first().type)
        empty_ips = StaticIPAddress.objects.filter(
            ip=None, alloc_type=IPADDRESS_TYPE.DISCOVERED, subnet=subnet)
        self.assertEqual(2, len(empty_ips))

    def test_update_leases_only_keeps_one_DISCOVERED_address(self):
        vlan = VLAN.objects.get_default_vlan()
        node = factory.make_Node_with_Interface_on_Subnet(vlan=vlan)
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()

        # Create DISCOVERED IP address that will be cleared.
        discovered_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            ip=str(IPAddress(ngi.get_dynamic_ip_range()[0])),
            subnet=subnet, interface=boot_interface)

        StaticIPAddress.objects.update_leases(node.nodegroup, [])
        discovered_ip = reload_object(discovered_ip)
        self.assertIsNone(discovered_ip.ip)

    def test_update_leases_drop_lease_and_unknown_interface(self):
        vlan = VLAN.objects.get_default_vlan()
        node = factory.make_Node_with_Interface_on_Subnet(vlan=vlan)
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()

        # Create UnknownInterface and DISCOVERED IP address that both
        # will be dropped.
        unknown_interface = UnknownInterface.objects.create(
            name="eth0", mac_address=factory.make_mac_address(),
            vlan=subnet.vlan)
        discovered_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            ip=str(IPAddress(ngi.get_dynamic_ip_range()[0])),
            subnet=subnet, interface=unknown_interface)

        StaticIPAddress.objects.update_leases(node.nodegroup, [])
        self.assertIsNone(reload_object(discovered_ip))
        self.assertIsNone(reload_object(unknown_interface))

    def test_update_leases_new_ip_existing_mac(self):
        vlan = VLAN.objects.get_default_vlan()
        node = factory.make_Node_with_Interface_on_Subnet(vlan=vlan)
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()

        ipaddr2 = str(IPAddress(ngi.get_dynamic_ip_range()[0]))
        StaticIPAddress.objects.update_leases(
            node.nodegroup, [(ipaddr2, boot_interface.mac_address)])
        new_ips = StaticIPAddress.objects.filter(ip=ipaddr2)
        self.assertThat(new_ips, HasLength(1))
        self.assertItemsEqual([boot_interface], new_ips[0].interface_set.all())

    def test_update_leases_new_ip_existing_mac_different_nodegroup(self):
        vlan = VLAN.objects.get_default_vlan()
        node = factory.make_Node_with_Interface_on_Subnet(vlan=vlan)
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()

        node2 = factory.make_Node_with_Interface_on_Subnet()
        ipaddr2 = str(IPAddress(ngi.get_dynamic_ip_range()[0]))
        StaticIPAddress.objects.update_leases(
            node2.nodegroup, [(ipaddr2, boot_interface.mac_address)])
        new_ips = StaticIPAddress.objects.filter(ip=ipaddr2)
        self.assertThat(new_ips, HasLength(1))
        self.assertItemsEqual([boot_interface], new_ips[0].interface_set.all())

    def test_update_leases_one_ip_two_mac(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()

        discovered_ip = factory.pick_ip_in_dynamic_range(ngi)
        discovered_mac1 = factory.make_mac_address()
        discovered_mac2 = factory.make_mac_address()

        StaticIPAddress.objects.update_leases(
            node.nodegroup,
            [(discovered_ip, discovered_mac1),
             (discovered_ip, discovered_mac2)])
        new_ips = StaticIPAddress.objects.filter(ip=discovered_ip)
        self.assertEqual(1, len(new_ips), StaticIPAddress.objects.all())
        self.assertEqual(IPADDRESS_TYPE.DISCOVERED, new_ips[0].alloc_type)
        self.assertEqual(
            [INTERFACE_TYPE.UNKNOWN, INTERFACE_TYPE.UNKNOWN],
            [iface.type for iface in new_ips[0].interface_set.all()])

    def test_update_leases_two_ip_one_mac(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        boot_interface = node.get_boot_interface()
        ip_address = boot_interface.ip_addresses.first()
        subnet = ip_address.subnet
        ngi = subnet.nodegroupinterface_set.first()

        discovered_ip1 = str(IPAddress(ngi.get_dynamic_ip_range()[0]))
        discovered_ip2 = str(IPAddress(ngi.get_dynamic_ip_range()[1]))
        discovered_mac = factory.make_mac_address()

        StaticIPAddress.objects.update_leases(
            node.nodegroup,
            [(discovered_ip1, discovered_mac),
             (discovered_ip2, discovered_mac)])
        new_ips = StaticIPAddress.objects.filter(ip=discovered_ip1)
        self.assertEqual(1, len(new_ips))
        self.assertEqual(
            IPADDRESS_TYPE.DISCOVERED, new_ips[0].alloc_type)
        self.assertEqual(
            INTERFACE_TYPE.UNKNOWN, new_ips[0].interface_set.first().type)
        new_ips2 = StaticIPAddress.objects.filter(ip=discovered_ip2)
        self.assertEqual(
            IPADDRESS_TYPE.DISCOVERED, new_ips2[0].alloc_type)
        self.assertEqual(
            INTERFACE_TYPE.UNKNOWN, new_ips2[0].interface_set.first().type)
        self.assertEqual(
            new_ips[0].interface_set.first(),
            new_ips2[0].interface_set.first())

    def test_filter_by_ip_family_ipv4(self):
        network_v4 = factory.make_ipv4_network()
        subnet_v4 = factory.make_Subnet(cidr=str(network_v4.cidr))
        ip_v4 = factory.pick_ip_in_network(network_v4)
        ip_v4 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip_v4,
            subnet=subnet_v4)
        network_v6 = factory.make_ipv6_network()
        subnet_v6 = factory.make_Subnet(cidr=str(network_v6.cidr))
        ip_v6 = factory.pick_ip_in_network(network_v6)
        ip_v6 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip_v6,
            subnet=subnet_v6)
        self.assertItemsEqual(
            [ip_v4],
            StaticIPAddress.objects.filter_by_ip_family(IPADDRESS_FAMILY.IPv4))

    def test_filter_by_ip_family_ipv6(self):
        network_v4 = factory.make_ipv4_network()
        subnet_v4 = factory.make_Subnet(cidr=str(network_v4.cidr))
        ip_v4 = factory.pick_ip_in_network(network_v4)
        ip_v4 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip_v4,
            subnet=subnet_v4)
        network_v6 = factory.make_ipv6_network()
        subnet_v6 = factory.make_Subnet(cidr=str(network_v6.cidr))
        ip_v6 = factory.pick_ip_in_network(network_v6)
        ip_v6 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip_v6,
            subnet=subnet_v6)
        self.assertItemsEqual(
            [ip_v6],
            StaticIPAddress.objects.filter_by_ip_family(IPADDRESS_FAMILY.IPv6))

    def test_filter_by_subnet_cidr_family_ipv4(self):
        network_v4 = factory.make_ipv4_network()
        subnet_v4 = factory.make_Subnet(cidr=str(network_v4.cidr))
        ip_v4 = factory.pick_ip_in_network(network_v4)
        ip_v4 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip_v4,
            subnet=subnet_v4)
        network_v6 = factory.make_ipv6_network()
        subnet_v6 = factory.make_Subnet(cidr=str(network_v6.cidr))
        ip_v6 = factory.pick_ip_in_network(network_v6)
        ip_v6 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip_v6,
            subnet=subnet_v6)
        self.assertItemsEqual(
            [ip_v4],
            StaticIPAddress.objects.filter_by_subnet_cidr_family(
                IPADDRESS_FAMILY.IPv4))

    def test_filter_by_subnet_cidr_family_ipv6(self):
        network_v4 = factory.make_ipv4_network()
        subnet_v4 = factory.make_Subnet(cidr=str(network_v4.cidr))
        ip_v4 = factory.pick_ip_in_network(network_v4)
        ip_v4 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip_v4,
            subnet=subnet_v4)
        network_v6 = factory.make_ipv6_network()
        subnet_v6 = factory.make_Subnet(cidr=str(network_v6.cidr))
        ip_v6 = factory.pick_ip_in_network(network_v6)
        ip_v6 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip_v6,
            subnet=subnet_v6)
        self.assertItemsEqual(
            [ip_v6],
            StaticIPAddress.objects.filter_by_subnet_cidr_family(
                IPADDRESS_FAMILY.IPv6))


class TestStaticIPAddressManagerTrasactional(MAASTransactionServerTestCase):
    '''The following TestStaticIPAddressManager tests require
        MAASTransactionServerTestCase, and thus have been separated
        from the TestStaticIPAddressManager above.
    '''

    def make_ip_ranges(self, network=None, nodegroup=None):
        if not nodegroup:
            nodegroup = factory.make_NodeGroup()
        interface = factory.make_NodeGroupInterface(
            nodegroup, management=NODEGROUPINTERFACE_MANAGEMENT.DHCP,
            network=network)
        return (
            interface.network,
            interface.static_ip_range_low,
            interface.static_ip_range_high,
            interface.ip_range_low,
            interface.ip_range_high,
        )

    @transactional
    def test_allocate_new_returns_ip_in_correct_range(self):
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        ipaddress = StaticIPAddress.objects.allocate_new(
            network, static_low, static_high, dynamic_low, dynamic_high)
        self.assertIsInstance(ipaddress, StaticIPAddress)
        iprange = IPRange(static_low, static_high)
        self.assertIn(IPAddress(ipaddress.ip), iprange)

    @transactional
    def test_allocate_new_allocates_IPv6_address(self):
        network = factory.make_ipv6_network()
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges(network))
        ipaddress = StaticIPAddress.objects.allocate_new(
            network, static_low, static_high, dynamic_low, dynamic_high)
        self.assertIsInstance(ipaddress, StaticIPAddress)
        self.assertIn(
            IPAddress(ipaddress.ip), IPRange(static_low, static_high))

    @transactional
    def test_allocate_new_sets_user(self):
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        user = factory.make_User()
        ipaddress = StaticIPAddress.objects.allocate_new(
            network, static_low, static_high, dynamic_low, dynamic_high,
            alloc_type=IPADDRESS_TYPE.USER_RESERVED, user=user)
        self.assertEqual(user, ipaddress.user)

    @transactional
    def test_allocate_new_with_user_disallows_wrong_alloc_types(self):
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        user = factory.make_User()
        alloc_type = factory.pick_enum(
            IPADDRESS_TYPE, but_not=[
                IPADDRESS_TYPE.DHCP,
                IPADDRESS_TYPE.DISCOVERED,
                IPADDRESS_TYPE.USER_RESERVED,
            ])
        self.assertRaises(
            AssertionError, StaticIPAddress.objects.allocate_new, network,
            static_low, static_high, dynamic_low, dynamic_high,
            user=user, alloc_type=alloc_type)

    @transactional
    def test_allocate_new_with_reserved_type_requires_a_user(self):
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        self.assertRaises(
            AssertionError, StaticIPAddress.objects.allocate_new, network,
            static_low, static_high, dynamic_low, dynamic_high,
            alloc_type=IPADDRESS_TYPE.USER_RESERVED)

    @transactional
    def test_allocate_new_compares_by_IP_not_alphabetically(self):
        # Django has a bug that casts IP addresses with HOST(), which
        # results in alphabetical comparisons of strings instead of IP
        # addresses.  See https://bugs.launchpad.net/maas/+bug/1338452
        network = "10.0.0.0/8"
        static_low = "10.0.0.98"
        static_high = "10.0.0.100"
        dynamic_low = "10.0.0.101"
        dynamic_high = "10.0.0.105"
        subnet = factory.make_Subnet(cidr=network)
        factory.make_StaticIPAddress("10.0.0.99", subnet=subnet)
        ipaddress = StaticIPAddress.objects.allocate_new(
            network, static_low, static_high, dynamic_low, dynamic_high,
            subnet=subnet)
        self.assertEqual(ipaddress.ip, "10.0.0.98")

    @transactional
    def test_allocate_new_returns_requested_IP_if_available(self):
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        requested_address = str(IPAddress(static_low) + 1)
        ipaddress = StaticIPAddress.objects.allocate_new(
            network, static_low, static_high, dynamic_low, dynamic_high,
            factory.pick_enum(
                IPADDRESS_TYPE, but_not=[
                    IPADDRESS_TYPE.DHCP,
                    IPADDRESS_TYPE.DISCOVERED,
                    IPADDRESS_TYPE.USER_RESERVED,
                ]),
            requested_address=requested_address)
        self.assertEqual(requested_address.format(), ipaddress.ip)

    @transactional
    def test_allocate_new_raises_when_requested_IP_unavailable(self):
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        requested_address = StaticIPAddress.objects.allocate_new(
            network, static_low, static_high, dynamic_low, dynamic_high,
            factory.pick_enum(
                IPADDRESS_TYPE, but_not=[
                    IPADDRESS_TYPE.DHCP,
                    IPADDRESS_TYPE.DISCOVERED,
                    IPADDRESS_TYPE.USER_RESERVED,
                ])).ip
        self.assertRaises(
            StaticIPAddressUnavailable, StaticIPAddress.objects.allocate_new,
            network, static_low, static_high, dynamic_low, dynamic_high,
            requested_address=requested_address)

    @transactional
    def test_allocate_new_raises_serialization_error_if_ip_taken(self):
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        # Simulate a "IP already taken" error.
        mock_attempt_allocation = self.patch(
            StaticIPAddress.objects, '_attempt_allocation')
        mock_attempt_allocation.side_effect = StaticIPAddressUnavailable()

        error = self.assertRaises(
            Exception, StaticIPAddress.objects.allocate_new,
            network, static_low, static_high, dynamic_low, dynamic_high)
        self.assertTrue(is_serialization_failure(error))

    @transactional
    def test_allocate_new_does_not_use_lock_for_requested_ip(self):
        # When requesting a specific IP address, there's no need to
        # acquire the lock.
        lock = self.patch(locks, 'staticip_acquire')
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        requested_address = str(IPAddress(static_low) + 1)
        ipaddress = StaticIPAddress.objects.allocate_new(
            network, static_low, static_high, dynamic_low, dynamic_high,
            requested_address=requested_address)
        self.assertIsInstance(ipaddress, StaticIPAddress)
        self.assertThat(lock.__enter__, MockNotCalled())

    @transactional
    def test_allocate_new_raises_when_requested_IP_out_of_network(self):
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        other_network = factory.make_ipv4_network(but_not=network)
        requested_address = factory.pick_ip_in_network(other_network)
        e = self.assertRaises(
            StaticIPAddressOutOfRange, StaticIPAddress.objects.allocate_new,
            network, static_low, static_high, dynamic_low, dynamic_high,
            factory.pick_enum(
                IPADDRESS_TYPE, but_not=[
                    IPADDRESS_TYPE.DHCP,
                    IPADDRESS_TYPE.DISCOVERED,
                    IPADDRESS_TYPE.USER_RESERVED,
                ]),
            requested_address=requested_address)
        self.assertEqual(
            "%s is not inside the network %s" % (
                requested_address, network),
            str(e))

    @transactional
    def test_allocate_new_raises_when_requested_IP_in_dynamic_range(self):
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        requested_address = dynamic_low
        e = self.assertRaises(
            StaticIPAddressOutOfRange, StaticIPAddress.objects.allocate_new,
            network, static_low, static_high, dynamic_low, dynamic_high,
            factory.pick_enum(
                IPADDRESS_TYPE, but_not=[
                    IPADDRESS_TYPE.DHCP,
                    IPADDRESS_TYPE.DISCOVERED,
                    IPADDRESS_TYPE.USER_RESERVED,
                ]),
            requested_address=requested_address)
        self.assertEqual(
            "%s is inside the dynamic range %s to %s" % (
                requested_address, dynamic_low, dynamic_high),
            str(e))

    @transactional
    def test_allocate_new_raises_when_alloc_type_is_None(self):
        error = self.assertRaises(
            ValueError, StaticIPAddress.objects.allocate_new,
            sentinel.network, sentinel.static_range_low,
            sentinel.static_range_low, sentinel.dynamic_range_low,
            sentinel.dynamic_range_high, alloc_type=None)
        self.assertEqual(
            "IP address type None is not allowed to use allocate_new.",
            str(error))

    @transactional
    def test_allocate_new_raises_when_alloc_type_is_not_allowed(self):
        error = self.assertRaises(
            ValueError, StaticIPAddress.objects.allocate_new,
            sentinel.network, sentinel.static_range_low,
            sentinel.static_range_low, sentinel.dynamic_range_low,
            sentinel.dynamic_range_high, alloc_type=IPADDRESS_TYPE.DHCP)
        self.assertEqual(
            "IP address type 5 is not allowed to use allocate_new.",
            str(error))

    @transactional
    def test_allocate_new_uses_staticip_acquire_lock(self):
        lock = self.patch(locks, 'staticip_acquire')
        network, static_low, static_high, dynamic_low, dynamic_high = (
            self.make_ip_ranges())
        ipaddress = StaticIPAddress.objects.allocate_new(
            network, static_low, static_high, dynamic_low, dynamic_high)
        self.assertIsInstance(ipaddress, StaticIPAddress)
        self.assertThat(lock.__enter__, MockCalledOnceWith())
        self.assertThat(
            lock.__exit__, MockCalledOnceWith(None, None, None))

    def test_allocate_new_raises_when_addresses_exhausted(self):
        network = "192.168.230.0/24"
        static_low = static_high = "192.168.230.1"
        dynamic_low = dynamic_high = "192.168.230.2"
        subnet = factory.make_Subnet(cidr=network)
        with transaction.atomic():
            StaticIPAddress.objects.allocate_new(
                network, static_low, static_high, dynamic_low, dynamic_high,
                subnet=subnet)
        with transaction.atomic():
            e = self.assertRaises(
                StaticIPAddressExhaustion,
                StaticIPAddress.objects.allocate_new,
                network, static_low, static_high, dynamic_low, dynamic_high,
                subnet=subnet)
        self.assertEqual(
            "No more IPs available in range %s-%s" % (static_low, static_high),
            str(e))


class TestStaticIPAddressManagerMapping(MAASServerTestCase):
    """Tests for get_hostname_ip_mapping()."""

    def test_get_hostname_ip_mapping_returns_mapping(self):
        self.patch_autospec(interface_module, "update_host_maps")
        domain = Domain.objects.get_default_domain()
        nodegroup = factory.make_NodeGroup(status=NODEGROUP_STATUS.ENABLED)
        expected_mapping = {}
        for _ in range(3):
            node = factory.make_Node_with_Interface_on_Subnet(
                nodegroup=nodegroup, disable_ipv4=False)
            [staticip] = node.get_boot_interface().claim_static_ips()
            full_hostname = "%s.%s" % (node.hostname, domain.name)
            expected_mapping[full_hostname] = [staticip.ip]
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(domain)
        self.assertEqual(expected_mapping, mapping)

    def test_get_hostname_ip_mapping_returns_fqdn(self):
        self.patch_autospec(interface_module, "update_host_maps")
        nodegroup = factory.make_NodeGroup(status=NODEGROUP_STATUS.ENABLED)
        hostname = factory.make_name('hostname')
        domainname = factory.make_name('domain')
        factory.make_Domain(name=domainname)
        full_hostname = "%s.%s" % (hostname, domainname)
        subnet = factory.make_Subnet()
        node = factory.make_Node_with_Interface_on_Subnet(
            nodegroup=nodegroup, hostname=full_hostname,
            subnet=subnet, disable_ipv4=False)
        [staticip] = node.get_boot_interface().claim_static_ips()
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(subnet)
        self.assertEqual({full_hostname: [staticip.ip]}, mapping)

    def test_get_hostname_ip_mapping_picks_mac_with_static_address(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), disable_ipv4=False)
        boot_interface = node.get_boot_interface()
        subnet = boot_interface.ip_addresses.first().subnet
        nic2 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node,
            vlan=boot_interface.vlan)
        staticip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, interface=nic2, subnet=subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [staticip.ip]}, mapping)

    def test_get_hostname_ip_mapping_considers_given_domain(self):
        nodegroup = factory.make_NodeGroup(status=NODEGROUP_STATUS.ENABLED)
        factory.make_Node_with_Interface_on_Subnet(nodegroup=nodegroup)
        domain = factory.make_Domain()
        another_nodegroup = factory.make_NodeGroup(name=domain.name)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            Domain.objects.get(name=another_nodegroup.name))
        self.assertEqual({}, mapping)

    def test_get_hostname_ip_mapping_picks_oldest_nic_with_sticky_ip(self):
        self.patch_autospec(interface_module, "update_host_maps")
        subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr))
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), subnet=subnet,
            disable_ipv4=False)
        [staticip] = node.get_boot_interface().claim_static_ips()
        newer_nic = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=newer_nic)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [staticip.ip]}, mapping)

    def test_get_hostname_ip_mapping_picks_sticky_over_auto(self):
        self.patch_autospec(interface_module, "update_host_maps")
        subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr))
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), subnet=subnet,
            disable_ipv4=False)
        [staticip] = node.get_boot_interface().claim_static_ips()
        nic = node.get_boot_interface()
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, interface=nic)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [staticip.ip]}, mapping)

    def test_get_hostname_ip_mapping_combines_IPv4_and_IPv6_addresses(self):
        node = factory.make_Node(interface=True, disable_ipv4=False)
        interface = node.get_boot_interface()
        ipv4_network = factory.make_ipv4_network()
        ipv4_subnet = factory.make_Subnet(cidr=ipv4_network)
        ipv4_address = factory.make_StaticIPAddress(
            interface=interface,
            ip=factory.pick_ip_in_network(ipv4_network), subnet=ipv4_subnet)
        ipv6_network = factory.make_ipv6_network()
        ipv6_subnet = factory.make_Subnet(cidr=ipv6_network)
        ipv6_address = factory.make_StaticIPAddress(
            interface=interface,
            ip=factory.pick_ip_in_network(ipv6_network), subnet=ipv6_subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertItemsEqual(
            [ipv4_address.ip, ipv6_address.ip],
            mapping[node.fqdn])

    def test_get_hostname_ip_mapping_combines_MACs_for_same_node(self):
        # A node's preferred static IPv4 and IPv6 addresses may be on
        # different MACs.
        node = factory.make_Node(disable_ipv4=False)
        ipv4_network = factory.make_ipv4_network()
        ipv4_subnet = factory.make_Subnet(cidr=ipv4_network)
        ipv4_address = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            interface=factory.make_Interface(
                INTERFACE_TYPE.PHYSICAL, node=node),
            ip=factory.pick_ip_in_network(ipv4_network), subnet=ipv4_subnet)
        ipv6_network = factory.make_ipv6_network()
        ipv6_subnet = factory.make_Subnet(cidr=ipv6_network)
        ipv6_address = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            interface=factory.make_Interface(
                INTERFACE_TYPE.PHYSICAL, node=node),
            ip=factory.pick_ip_in_network(ipv6_network), subnet=ipv6_subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertItemsEqual(
            [ipv4_address.ip, ipv6_address.ip],
            mapping[node.fqdn])

    def test_get_hostname_ip_mapping_skips_ipv4_if_disable_ipv4_set(self):
        node = factory.make_Node(interface=True, disable_ipv4=True)
        boot_interface = node.get_boot_interface()
        ipv4_network = factory.make_ipv4_network()
        ipv4_subnet = factory.make_Subnet(cidr=ipv4_network)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            interface=boot_interface,
            ip=factory.pick_ip_in_network(ipv4_network), subnet=ipv4_subnet)
        ipv6_network = factory.make_ipv6_network()
        ipv6_subnet = factory.make_Subnet(cidr=ipv6_network)
        ipv6_address = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            interface=boot_interface,
            ip=factory.pick_ip_in_network(ipv6_network), subnet=ipv6_subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [ipv6_address.ip]}, mapping)

    def test_get_hostname_ip_mapping_prefers_non_discovered_addresses(self):
        self.patch_autospec(interface_module, "update_host_maps")
        subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr))
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), subnet=subnet,
            disable_ipv4=False)
        iface = node.get_boot_interface()
        staticip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, interface=iface,
            subnet=subnet)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, interface=iface,
            subnet=subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [staticip.ip]}, mapping)

    def test_get_hostname_ip_mapping_prefers_bond_with_no_boot_interface(self):
        self.patch_autospec(interface_module, "update_host_maps")
        subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr))
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), subnet=subnet,
            disable_ipv4=False)
        node.boot_interface = None
        node.save()
        iface = node.get_boot_interface()
        iface2 = factory.make_Interface(node=node)
        iface3 = factory.make_Interface(node=node)
        bondif = factory.make_Interface(
            INTERFACE_TYPE.BOND, node=node, parents=[iface2, iface3])
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface,
            subnet=subnet)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface2,
            subnet=subnet)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface3,
            subnet=subnet)
        bond_staticip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=bondif,
            subnet=subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [bond_staticip.ip]}, mapping)

    def test_get_hostname_ip_mapping_prefers_bond_with_boot_interface(self):
        self.patch_autospec(interface_module, "update_host_maps")
        subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr))
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), subnet=subnet,
            disable_ipv4=False)
        iface = node.get_boot_interface()
        iface2 = factory.make_Interface(node=node)
        bondif = factory.make_Interface(
            INTERFACE_TYPE.BOND, node=node, parents=[iface, iface2])
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface,
            subnet=subnet)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface2,
            subnet=subnet)
        bond_staticip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=bondif,
            subnet=subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [bond_staticip.ip]}, mapping)

    def test_get_hostname_ip_mapping_ignores_bond_without_boot_interface(self):
        self.patch_autospec(interface_module, "update_host_maps")
        subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr))
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), subnet=subnet,
            disable_ipv4=False)
        iface = node.get_boot_interface()
        iface2 = factory.make_Interface(node=node)
        iface3 = factory.make_Interface(node=node)
        bondif = factory.make_Interface(
            INTERFACE_TYPE.BOND, node=node, parents=[iface2, iface3])
        boot_staticip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface,
            subnet=subnet)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface2,
            subnet=subnet)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface3,
            subnet=subnet)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=bondif,
            subnet=subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [boot_staticip.ip]}, mapping)

    def test_get_hostname_ip_mapping_prefers_boot_interface(self):
        self.patch_autospec(interface_module, "update_host_maps")
        subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr))
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), subnet=subnet,
            disable_ipv4=False)
        iface = node.get_boot_interface()
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface,
            subnet=subnet)
        new_boot_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node)
        node.boot_interface = new_boot_interface
        node.save()
        # IP address should be selected over the other physical IP address.
        boot_sip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=new_boot_interface,
            subnet=subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [boot_sip.ip]}, mapping)

    def test_get_hostname_ip_mapping_prefers_boot_interface_to_alias(self):
        self.patch_autospec(interface_module, "update_host_maps")
        subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr))
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), subnet=subnet,
            disable_ipv4=False)
        iface = node.get_boot_interface()
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface,
            subnet=subnet)
        new_boot_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node)
        node.boot_interface = new_boot_interface
        node.save()
        # IP address should be selected over the other STICKY IP address.
        boot_sip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, interface=new_boot_interface,
            subnet=subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [boot_sip.ip]}, mapping)

    def test_get_hostname_ip_mapping_prefers_physical_interfaces_to_vlan(self):
        self.patch_autospec(interface_module, "update_host_maps")
        subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr))
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=factory.make_name('host'), subnet=subnet,
            disable_ipv4=False)
        iface = node.get_boot_interface()
        vlanif = factory.make_Interface(
            INTERFACE_TYPE.VLAN, node=node, parents=[iface])
        phy_staticip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=iface,
            subnet=subnet)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=vlanif,
            subnet=subnet)
        mapping = StaticIPAddress.objects.get_hostname_ip_mapping(
            node.domain)
        self.assertEqual({node.fqdn: [phy_staticip.ip]}, mapping)


class TestStaticIPAddress(MAASServerTestCase):

    def test_repr_with_valid_type(self):
        # Using USER_RESERVED here because it doesn't validate the Subnet.
        actual = "%s" % factory.make_StaticIPAddress(
            ip="10.0.0.1", alloc_type=IPADDRESS_TYPE.USER_RESERVED)
        self.assertEqual("10.0.0.1:type=USER_RESERVED", actual)

    def test_repr_with_invalid_type(self):
        actual = "%s" % factory.make_StaticIPAddress(
            ip="10.0.0.1", alloc_type=99999, subnet=factory.make_Subnet(
                cidr="10.0.0.0/8"))
        self.assertEqual("10.0.0.1:type=99999", actual)

    def test_stores_to_database(self):
        ipaddress = factory.make_StaticIPAddress()
        self.assertEqual([ipaddress], list(StaticIPAddress.objects.all()))

    def test_invalid_address_raises_validation_error(self):
        ip = StaticIPAddress(ip='256.0.0.0.0')
        self.assertRaises(ValidationError, ip.full_clean)

    def test_get_interface_link_type_returns_AUTO_for_AUTO(self):
        ip = StaticIPAddress(alloc_type=IPADDRESS_TYPE.AUTO)
        self.assertEqual(
            INTERFACE_LINK_TYPE.AUTO, ip.get_interface_link_type())

    def test_get_interface_link_type_returns_DHCP_for_DHCP(self):
        ip = StaticIPAddress(alloc_type=IPADDRESS_TYPE.DHCP)
        self.assertEqual(
            INTERFACE_LINK_TYPE.DHCP, ip.get_interface_link_type())

    def test_get_interface_link_type_returns_STATIC_for_USER_RESERVED(self):
        ip = StaticIPAddress(alloc_type=IPADDRESS_TYPE.USER_RESERVED)
        self.assertEqual(
            INTERFACE_LINK_TYPE.STATIC, ip.get_interface_link_type())

    def test_get_interface_link_type_returns_STATIC_for_STICKY_with_ip(self):
        ip = StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=factory.make_ipv4_address())
        self.assertEqual(
            INTERFACE_LINK_TYPE.STATIC, ip.get_interface_link_type())

    def test_get_interface_link_type_returns_LINK_UP_for_STICKY_no_ip(self):
        ip = StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip="")
        self.assertEqual(
            INTERFACE_LINK_TYPE.LINK_UP, ip.get_interface_link_type())

    def test_deallocate_removes_object(self):
        ipaddress = factory.make_StaticIPAddress()
        ipaddress.deallocate()
        self.assertEqual([], list(StaticIPAddress.objects.all()))

    def test_deallocate_ignores_other_objects(self):
        ipaddress = factory.make_StaticIPAddress()
        ipaddress2 = factory.make_StaticIPAddress()
        ipaddress.deallocate()
        self.assertEqual([ipaddress2], list(StaticIPAddress.objects.all()))


class TestUserReservedStaticIPAddress(MAASServerTestCase):

    def test_user_reserved_addresses_have_default_hostnames(self):
        num_ips = randint(3, 5)
        ips = [
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.USER_RESERVED)
            for _ in range(num_ips)
        ]
        mappings = StaticIPAddress.objects._get_user_reserved_mappings()
        self.expectThat(mappings, HasLength(len(ips)))

    def test_user_reserved_addresses_included_in_get_hostname_ip_mapping(self):
        num_ips = randint(3, 5)
        nodegroup = factory.make_NodeGroup(name=factory.make_Domain().name)
        ips = [
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.USER_RESERVED)
            for _ in range(num_ips)
        ]
        mappings = StaticIPAddress.objects.get_hostname_ip_mapping(
            Domain.objects.get(name=nodegroup.name))
        self.expectThat(mappings, HasLength(len(ips)))

    def test_user_reserved_addresses_included_in_all_nodegroups(self):
        num_ips = randint(3, 5)
        nodegroup1 = factory.make_NodeGroup(name=factory.make_Domain().name)
        nodegroup2 = factory.make_NodeGroup(name=factory.make_Domain().name)
        ips = [
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.USER_RESERVED)
            for _ in range(num_ips)
        ]
        mappings = StaticIPAddress.objects.get_hostname_ip_mapping(
            Domain.objects.get(name=nodegroup1.name))
        self.expectThat(mappings, HasLength(len(ips)))
        mappings = StaticIPAddress.objects.get_hostname_ip_mapping(
            Domain.objects.get(name=nodegroup2.name))
        self.expectThat(mappings, HasLength(len(ips)))


class TestRenderJSON(MAASServerTestCase):

    def test__excludes_username_and_node_summary_by_default(self):
        ip = factory.make_StaticIPAddress(
            ip=factory.make_ipv4_address(),
            alloc_type=IPADDRESS_TYPE.USER_RESERVED)
        json = ip.render_json()
        self.expectThat(json, Not(Contains("user")))
        self.expectThat(json, Not(Contains("node_summary")))

    def test__includes_username_if_requested(self):
        user = factory.make_User()
        ip = factory.make_StaticIPAddress(
            ip=factory.make_ipv4_address(), user=user,
            alloc_type=IPADDRESS_TYPE.USER_RESERVED)
        json = ip.render_json(with_username=True)
        self.expectThat(json, Contains("user"))
        self.expectThat(json, Not(Contains("node_summary")))
        self.expectThat(json["user"], Equals(user.username))

    def test__includes_node_summary_if_requested(self):
        user = factory.make_User()
        node = factory.make_Node_with_Interface_on_Subnet()
        ngi = node.get_boot_interface().get_cluster_interface()
        ip = factory.make_StaticIPAddress(
            ip=factory.pick_ip_in_dynamic_range(ngi), user=user,
            interface=node.get_boot_interface())
        json = ip.render_json(with_node_summary=True)
        self.expectThat(json, Not(Contains("user")))
        self.expectThat(json, Contains("node_summary"))

    def test__data_is_accurate(self):
        user = factory.make_User()
        hostname = factory.make_name('hostname')
        node = factory.make_Node_with_Interface_on_Subnet(
            hostname=hostname)
        ngi = node.get_boot_interface().get_cluster_interface()
        ip = factory.make_StaticIPAddress(
            ip=factory.pick_ip_in_dynamic_range(ngi), user=user,
            interface=node.get_boot_interface())
        json = ip.render_json(with_username=True, with_node_summary=True)
        self.expectThat(json["user"], Equals(user.username))
        self.assertThat(json, Contains("node_summary"))
        node_summary = json["node_summary"]
        self.expectThat(node_summary["system_id"], Equals(node.system_id))
        self.expectThat(node_summary["node_type"], Equals(node.node_type))
