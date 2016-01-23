# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for Domain API."""

__all__ = []

import http.client
import json
import random

from django.conf import settings
from django.core.urlresolvers import reverse
from maasserver.models.domain import Domain
from maasserver.testing.api import APITestCase
from maasserver.testing.factory import factory
from maasserver.testing.orm import reload_object
from testtools.matchers import (
    ContainsDict,
    Equals,
)


def get_domains_uri():
    """Return a Domain's URI on the API."""
    return reverse('domains_handler', args=[])


def get_domain_uri(domain):
    """Return a Domain URI on the API."""
    return reverse(
        'domain_handler', args=[domain.id])


class TestDomainsAPI(APITestCase):

    def test_handler_path(self):
        self.assertEqual(
            '/api/2.0/domains/', get_domains_uri())

    def test_read(self):
        for _ in range(3):
            factory.make_Domain()
        uri = get_domains_uri()
        response = self.client.get(uri)

        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        expected_ids = [
            domain.id
            for domain in Domain.objects.all()
            ]
        result_ids = [
            domain["id"]
            for domain in json.loads(
                response.content.decode(settings.DEFAULT_CHARSET))
            ]
        self.assertItemsEqual(expected_ids, result_ids)

    def test_create(self):
        self.become_admin()
        domain_name = factory.make_name("domain")
        uri = get_domains_uri()
        response = self.client.post(uri, {
            "name": domain_name,
        })
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertEqual(
            domain_name,
            json.loads(
                response.content.decode(settings.DEFAULT_CHARSET))['name'])

    def test_create_admin_only(self):
        domain_name = factory.make_name("domain")
        uri = get_domains_uri()
        response = self.client.post(uri, {
            "name": domain_name,
        })
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)

    def test_create_requires_name(self):
        self.become_admin()
        uri = get_domains_uri()
        response = self.client.post(uri, {})
        self.assertEqual(
            http.client.BAD_REQUEST, response.status_code, response.content)


class TestDomainAPI(APITestCase):

    def test_handler_path(self):
        domain = factory.make_Domain()
        self.assertEqual(
            '/api/2.0/domains/%s/' % domain.id,
            get_domain_uri(domain))

    def test_read(self):
        domain = factory.make_Domain()
        dnsrr_ids = [
            factory.make_DNSResource(domain=domain).id
            for _ in range(3)
        ]
        uri = get_domain_uri(domain)
        response = self.client.get(uri)

        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        parsed_domain = json.loads(
            response.content.decode(settings.DEFAULT_CHARSET))
        self.assertThat(parsed_domain, ContainsDict({
            "id": Equals(domain.id),
            "name": Equals(domain.get_name()),
            }))
        parsed_dnsrrs = [
            dnsrr["id"]
            for dnsrr in parsed_domain["resources"]
        ]
        self.assertItemsEqual(dnsrr_ids, parsed_dnsrrs)

    def test_read_404_when_bad_id(self):
        uri = reverse(
            'domain_handler', args=[random.randint(100, 1000)])
        response = self.client.get(uri)
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)

    def test_update(self):
        self.become_admin()
        domain = factory.make_Domain()
        new_name = factory.make_name("domain")
        uri = get_domain_uri(domain)
        response = self.client.put(uri, {
            "name": new_name,
        })
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertEqual(
            new_name,
            json.loads(
                response.content.decode(settings.DEFAULT_CHARSET))['name'])
        self.assertEqual(new_name, reload_object(domain).name)

    def test_update_admin_only(self):
        domain = factory.make_Domain()
        new_name = factory.make_name("domain")
        uri = get_domain_uri(domain)
        response = self.client.put(uri, {
            "name": new_name,
        })
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)

    def test_delete_deletes_domain(self):
        self.become_admin()
        domain = factory.make_Domain()
        uri = get_domain_uri(domain)
        response = self.client.delete(uri)
        self.assertEqual(
            http.client.NO_CONTENT, response.status_code, response.content)
        self.assertIsNone(reload_object(domain))

    def test_delete_403_when_not_admin(self):
        domain = factory.make_Domain()
        uri = get_domain_uri(domain)
        response = self.client.delete(uri)
        self.assertEqual(
            http.client.FORBIDDEN, response.status_code, response.content)
        self.assertIsNotNone(reload_object(domain))

    def test_delete_404_when_invalid_id(self):
        self.become_admin()
        uri = reverse(
            'domain_handler', args=[random.randint(100, 1000)])
        response = self.client.delete(uri)
        self.assertEqual(
            http.client.NOT_FOUND, response.status_code, response.content)
