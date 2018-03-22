#
# Copyright 2018 Lubomir Rintel <lkundrak@v3.sk>
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

import logging
import os
import re

from . import urlfetcher
from . import util
from .installer import Installer

class LXCInstaller(Installer):

    def __init__(self, *args, **kwargs):
        Installer.__init__(self, *args, **kwargs)

        self._init = None
        self._initargs = None
        self._filesystems = None
        self._interfaces = None
        self._privnet = None
        self._target_tree = None
        self._store = None
        self._repo_type = None
        self._repo_url = None
        self._repo_version = None

    def _get_store(self, guest):
        if not self._store:
            scratchdir = util.make_scratchdir(guest.conn, guest.type)
            meter = util.make_meter(quiet=True)
            fetcher = urlfetcher.fetcherForURI(self.location, scratchdir, meter)
            fetcher.prepareLocation()
            self._store = urlfetcher.getDistroStore(guest, fetcher)
        return self._store

    ######################################################
    # Back up and restore settings for the install phase #
    ######################################################

    def _save_installed_config(self, guest):
        self._init = guest.os.init
        self._initargs = guest.os.initargs
        self._filesystems = guest.get_devices("filesystem")
        self._interfaces = guest.get_devices("interface")
        self._privnet = guest.features.privnet

    def _remove_installed_config(self, guest):
        # The install phase will use a bootstrapper for init
        for initarg in guest.os.initargs:
            guest.os.remove_child(initarg)
        # The bootstrapper will run from the host installation
        for filesystem in guest.get_devices("filesystem"):
            guest.remove_device(filesystem)
        # The bootstrapper will use the host networking
        guest.features.privnet = False
        for interface in guest.get_devices("interface"):
            guest.remove_device(interface)

    def _restore_installed_config(self, guest):
        guest.features.privnet = self._privnet
        guest.os.init = self._init
        for initarg in self._initargs:
            guest.os.add_child(initarg)
        for filesystem in self._filesystems:
            guest.add_device(filesystem)
        for interface in self._interfaces:
            guest.add_device(interface)

    ########################################
    # Run the bootstrap in installer phase #
    ########################################

    ##########################
    # Public installer impls #
    ##########################

    def detect_distro(self, guest):
        return self._get_store(guest).get_osdict_info()

    def has_install_phase(self):
        return True

    def prepare(self, guest, meter):
        self._save_installed_config(guest)

        for filesystem in self._filesystems:
            if self._filesystems[0].target == "/":
                self._target_tree = self._filesystems[0].source
        if not self._target_tree:
            raise ValueError(_("Need a filesystem to install to."))
        try:
            os.mkdir(self._target_tree)
        except FileExistsError:
            pass

        uid  = guest.idmap.uid_target
        if not uid:
            uid = -1
        gid  = guest.idmap.gid_target
        if not gid:
            gid = -1
        os.chown(self._target_tree, uid, gid)

        self._repo_type, self._repo_url, self._repo_version = self._get_store(guest).getRepoData()

    def alter_bootconfig(self, guest, isinstall):
        self._remove_installed_config(guest)

        if isinstall:
            if True:
                raise ValueError(_("%s repositories are not supported") % self._repo_type)
        else:
            self._restore_installed_config(guest)
