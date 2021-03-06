# Copyright (C) 2013, 2014 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.

import difflib
import os
import sys
import unittest

import libvirt

import virtinst
import virtinst.cli
import virtinst.uri


# pylint: disable=protected-access
# Access to protected member, needed to unittest stuff

class _CLIState(object):
    """
    Class containing any bits passed in from setup.py
    """
    def __init__(self):
        self.regenerate_output = False
        self.use_coverage = False
        self.debug = False
clistate = _CLIState()


_capsprefix  = ",caps=%s/tests/capabilities-xml/" % os.getcwd()
_domcapsprefix  = ",domcaps=%s/tests/capabilities-xml/" % os.getcwd()

uri_test_default = "__virtinst_test__test:///default,predictable"
uri_test_full = "__virtinst_test__test:///%s/tests/testdriver.xml,predictable" % os.getcwd()
uri_test_suite = "__virtinst_test__test:///%s/tests/testsuite.xml,predictable" % os.getcwd()
uri_test = uri_test_full
uri_test_remote = uri_test + ",remote"

_uri_qemu = "%s,qemu" % uri_test
_uri_kvm_domcaps = (_uri_qemu + _domcapsprefix + "kvm-x86_64-domcaps.xml")
_uri_kvm_domcaps_q35 = (_uri_qemu + _domcapsprefix + "kvm-x86_64-domcaps-q35.xml")
_uri_kvm_aarch64_domcaps = (_uri_qemu + _domcapsprefix + "kvm-aarch64-domcaps.xml")
uri_kvm_nodomcaps = (_uri_qemu + _capsprefix + "kvm-x86_64.xml")
uri_kvm_rhel = (_uri_kvm_domcaps + _capsprefix + "kvm-x86_64-rhel7.xml")
uri_kvm = (_uri_kvm_domcaps + _capsprefix + "kvm-x86_64.xml")
uri_kvm_q35 = (_uri_kvm_domcaps_q35 + _capsprefix + "kvm-x86_64.xml")
uri_kvm_session = uri_kvm + ",session"

uri_kvm_armv7l = (_uri_kvm_domcaps + _capsprefix + "kvm-armv7l.xml")
uri_kvm_aarch64 = (_uri_kvm_aarch64_domcaps + _capsprefix + "kvm-aarch64.xml")
uri_kvm_ppc64le = (_uri_kvm_domcaps + _capsprefix + "kvm-ppc64le.xml")
uri_kvm_s390x = (_uri_kvm_domcaps + _capsprefix + "kvm-s390x.xml")
uri_kvm_s390x_KVMIBM = (_uri_kvm_domcaps + _capsprefix + "kvm-s390x-KVMIBM.xml")

uri_xen = uri_test + _capsprefix + "xen-rhel5.4.xml,xen"
uri_lxc = uri_test + _capsprefix + "lxc.xml,lxc"
uri_vz = uri_test + _capsprefix + "vz.xml,vz"


def _make_uri(base, connver=None, libver=None):
    if connver:
        base += ",connver=%s" % connver
    if libver:
        base += ",libver=%s" % libver
    return base


class _URIs(object):
    def __init__(self):
        self._conn_cache = {}
        self._testdriver_cache = None
        self._testdriver_error = None
        self._testdriver_default = None

    def openconn(self, uri):
        """
        Extra super caching to speed up the test suite. We basically
        cache the first guest/pool/vol poll attempt for each URI, and save it
        across multiple reopenings of that connection. We aren't caching
        libvirt objects, just parsed XML objects. This works fine since
        generally every test uses a fresh virConnect, or undoes the
        persistent changes it makes.
        """
        virtinst.util.register_libvirt_error_handler()
        is_testdriver_xml = "/testdriver.xml" in uri

        if not (is_testdriver_xml and self._testdriver_error):
            try:
                conn = virtinst.cli.getConnection(uri)
            except libvirt.libvirtError as e:
                if not is_testdriver_xml:
                    raise
                self._testdriver_error = (
                        "error opening testdriver.xml: %s\n"
                        "libvirt is probably too old" % str(e))
                print(self._testdriver_error, file=sys.stderr)

        if is_testdriver_xml and self._testdriver_error:
            raise unittest.SkipTest(self._testdriver_error)

        uri = conn._open_uri

        # For the basic test:///default URI, skip this caching, so we have
        # an option to test the stock code
        if uri == uri_test_default:
            return conn

        if uri not in self._conn_cache:
            conn.fetch_all_guests()
            conn.fetch_all_pools()
            conn.fetch_all_vols()
            conn.fetch_all_nodedevs()

            self._conn_cache[uri] = {}
            for key, value in conn._fetch_cache.items():
                self._conn_cache[uri][key] = value[:]

        # Prime the internal connection cache
        for key, value in self._conn_cache[uri].items():
            conn._fetch_cache[key] = value[:]

        def cb_cache_new_pool(poolobj):
            # Used by clonetest.py nvram-newpool test
            if poolobj.name() == "nvram-newpool":
                from virtinst import StorageVolume
                vol = StorageVolume(conn)
                vol.pool = poolobj
                vol.name = "clone-orig-vars.fd"
                vol.capacity = 1024 * 1024
                vol.install()
            conn._cache_new_pool_raw(poolobj)

        conn.cb_cache_new_pool = cb_cache_new_pool

        return conn

    def open_testdriver_cached(self):
        """
        Open plain testdriver.xml and cache the instance. Tests that
        use this are expected to clean up after themselves so driver
        state doesn't become polluted.
        """
        if not self._testdriver_cache:
            self._testdriver_cache = self.openconn(uri_test)
        return self._testdriver_cache

    def open_testdefault_cached(self):
        if not self._testdriver_default:
            self._testdriver_default = self.openconn(uri_test_default)
        return self._testdriver_default

    def open_kvm(self, connver=None, libver=None):
        return self.openconn(_make_uri(uri_kvm, connver, libver))
    def open_kvm_rhel(self, connver=None):
        return self.openconn(_make_uri(uri_kvm_rhel, connver))
    def open_test_remote(self):
        return self.openconn(uri_test_remote)

URIs = _URIs()



def test_create(testconn, xml, define_func="defineXML"):
    xml = virtinst.uri.sanitize_xml_for_test_define(xml)

    try:
        func = getattr(testconn, define_func)
        obj = func(xml)
    except Exception as e:
        raise RuntimeError(str(e) + "\n" + xml)

    try:
        obj.create()
        obj.destroy()
        obj.undefine()
    except Exception:
        try:
            obj.destroy()
        except Exception:
            pass
        try:
            obj.undefine()
        except Exception:
            pass


def read_file(filename):
    """Helper function to read a files contents and return them"""
    f = open(filename, "r")
    out = f.read()
    f.close()

    return out


def diff_compare(actual_out, filename=None, expect_out=None):
    """Compare passed string output to contents of filename"""
    if not expect_out:
        if not os.path.exists(filename) or clistate.regenerate_output:
            open(filename, "w").write(actual_out)
        expect_out = read_file(filename)

    diff = "".join(difflib.unified_diff(expect_out.splitlines(1),
                                        actual_out.splitlines(1),
                                        fromfile=filename or '',
                                        tofile="Generated Output"))
    if diff:
        raise AssertionError("Conversion outputs did not match.\n%s" % diff)
