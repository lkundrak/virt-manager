#
# Common code for all guests
#
# Copyright 2006-2009, 2013  Red Hat, Inc.
# Jeremy Katz <katzj@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free  Software Foundation; either version 2 of the License, or
# (at your option)  any later version.
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

import urlgrabber.progress as progress
import libvirt

import virtinst
from virtinst import util
from virtinst import support
from virtinst import OSXML
from virtinst import VirtualDisk
from virtinst import VirtualDevice
from virtinst import Clock
from virtinst import Seclabel
from virtinst import CPU
from virtinst import DomainNumatune
from virtinst import DomainFeatures
from virtinst.xmlbuilder import XMLBuilder, XMLProperty

from virtinst import osdict


class Guest(XMLBuilder):

    # OS Dictionary static variables and methods
    _DEFAULTS = osdict.DEFAULTS
    _OS_TYPES = osdict.OS_TYPES

    @staticmethod
    def pretty_os_list():
        """
        Return a strip representation of OS list for printing
        """
        ret = ""
        for t in Guest.list_os_types():
            for v in Guest.list_os_variants(t):
                ret += "%-20s : %s\n" % (v, Guest.get_os_variant_label(t, v))
        return ret

    @staticmethod
    def list_os_types(supported=False, filtervars=None):
        """
        @param filtervars: List of only variants we want to show by default
        """
        vals = osdict.sort_helper(Guest._OS_TYPES)
        for t in vals[:]:
            if not Guest.list_os_variants(t, supported=supported,
                                          filtervars=filtervars):
                vals.remove(t)
        return vals

    @staticmethod
    def list_os_variants(typ, sortpref=None, supported=False, filtervars=None):
        """
        Return a list of sorted os variants for the passed distro type

        @param sortpref: An option list of osdict 'distro' tags to
        prioritize in the returned list, e.g. passing ["fedora"] will make
        the sorted list have all fedora distros first
        @param filtervars: List of only variants we want to show by default
        """
        vals = osdict.sort_helper(Guest._OS_TYPES[typ]["variants"],
                                  sortpref)
        ret = []
        for v in vals:
            if filtervars:
                if v not in filtervars:
                    continue
            elif supported:
                if not osdict.lookup_osdict_key(None, None,
                                                typ, v, "supported"):
                    continue

            ret.append(v)
        return ret

    @staticmethod
    def get_os_type_label(typ):
        return Guest._OS_TYPES[typ]["label"]

    @staticmethod
    def get_os_variant_label(typ, variant):
        return Guest._OS_TYPES[typ]["variants"][variant]["label"]

    @staticmethod
    def cpuset_str_to_tuple(conn, cpuset):
        return DomainNumatune.cpuset_str_to_tuple(conn, cpuset)

    @staticmethod
    def generate_cpuset(conn, mem):
        """
        Generates a cpu pinning string based on host NUMA configuration.

        If host doesn't have a suitable NUMA configuration, a RuntimeError
        is thrown.
        """
        if conn.caps.host.topology is None:
            raise RuntimeError(_("No topology section in capabilities xml."))

        cells = conn.caps.host.topology.cells
        if len(cells) <= 1:
            raise RuntimeError(_("Capabilities only show <= 1 cell. "
                                 "Not NUMA capable"))

        # Capabilities tells us about the available memory 'cells' on the
        # system. Each 'cell' has associated 'cpu's.
        #
        # Use getCellsFreeMemory to determine which 'cell' has the smallest
        # amount of memory which fits the requested VM memory amount, then
        # pin the VM to that 'cell's associated 'cpu's

        cell_mem = conn.getCellsFreeMemory(0, len(cells))
        cell_id = -1
        for i in range(len(cells)):
            if cell_mem[i] < mem:
                # Cell doesn't have enough mem to fit, skip it
                continue

            if len(cells[i].cpus) == 0:
                # No cpus to use for the cell
                continue

            # Find smallest cell that fits
            if cell_id < 0 or cell_mem[i] < cell_mem[cell_id]:
                cell_id = i

        if cell_id < 0:
            raise RuntimeError(_("Could not find any usable NUMA "
                                 "cell/cpu combinations."))

        # Build cpuset string
        cpustr = ""
        for cpu in cells[cell_id].cpus:
            if cpustr != "":
                cpustr += ","
            cpustr += str(cpu.id)

        return cpustr

    @staticmethod
    def check_vm_collision(conn, name, do_remove):
        """
        Remove the existing VM with the same name if requested, or error
        if there is a collision.
        """
        vm = None
        try:
            vm = conn.lookupByName(name)
        except libvirt.libvirtError:
            pass

        if vm is None:
            return

        if not do_remove:
            raise RuntimeError(_("Domain named %s already exists!") % name)

        try:
            logging.debug("Explicitly replacing guest '%s'", name)
            if vm.ID() != -1:
                logging.info("Destroying guest '%s'", name)
                vm.destroy()

            logging.info("Undefining guest '%s'", name)
            vm.undefine()
        except libvirt.libvirtError, e:
            raise RuntimeError(_("Could not remove old vm '%s': %s") %
                               (str(e)))

    @staticmethod
    def validate_name(conn, name, check_collision):
        util.validate_name(_("Guest"), name, lencheck=True)
        if not check_collision:
            return

        try:
            conn.lookupByName(name)
        except:
            return
        raise ValueError(_("Guest name '%s' is already in use.") % name)


    _XML_ROOT_XPATH = "/domain"
    _XML_PROP_ORDER = ["type", "name", "uuid", "description",
        "maxmemory", "memory", "hugepage", "vcpus", "curvcpus",
        "numatune", "bootloader", "os", "features", "cpu", "clock",
        "on_poweroff", "on_reboot", "on_crash", "emulator", "all_devices",
        "seclabel"]

    def __init__(self, conn, parsexml=None, parsexmlnode=None):
        self._devices = []
        self._install_devices = []
        XMLBuilder.__init__(self, conn, parsexml, parsexmlnode)

        self.autostart = False
        self.replace = False
        self.os_autodetect = False

        self._os_type = None
        self._os_variant = None
        self._random_uuid = None

        # The libvirt virDomain object we 'Create'
        self.domain = None

        self.installer = virtinst.DistroInstaller(conn)
        self.os = OSXML(self.conn, None, self._xml_node)
        self.features = DomainFeatures(self.conn, None, self._xml_node)
        self.clock = Clock(self.conn, None, self._xml_node)
        self.seclabel = Seclabel(self.conn, None, self._xml_node)
        self.cpu = CPU(self.conn, None, self._xml_node)
        self.numatune = DomainNumatune(self.conn, None, self._xml_node)


    ######################
    # Property accessors #
    ######################

    def _validate_name(self, val):
        if val == self.name:
            return
        self.validate_name(self.conn, val, check_collision=not self.replace)
    name = XMLProperty(xpath="./name", validate_cb=_validate_name)

    def _set_memory(self, val):
        if val is None:
            return None

        if self.maxmemory is None or self.maxmemory < val:
            self.maxmemory = val
        return val
    memory = XMLProperty(xpath="./currentMemory", is_int=True,
                         default_cb=lambda s: 1,
                         set_converter=_set_memory)
    maxmemory = XMLProperty(xpath="./memory", is_int=True)

    def _set_vcpus(self, val):
        if val is None:
            return None

        # Don't force set curvcpus unless already specified
        if self.curvcpus is not None and self.curvcpus > val:
            self.curvcpus = val
        return val
    vcpus = XMLProperty(xpath="./vcpu", is_int=True,
                        set_converter=_set_vcpus,
                        default_cb=lambda s: 1)
    curvcpus = XMLProperty(xpath="./vcpu/@current", is_int=True)

    def _validate_cpuset(self, val):
        DomainNumatune.validate_cpuset(self.conn, val)
    cpuset = XMLProperty(xpath="./vcpu/@cpuset",
                         validate_cb=_validate_cpuset)

    def _get_default_uuid(self):
        if self._random_uuid is None:
            self._random_uuid = util.generate_uuid(self.conn)
        return self._random_uuid
    uuid = XMLProperty(xpath="./uuid",
                       validate_cb=lambda s, v: util.validate_uuid(v),
                       default_cb=_get_default_uuid)

    type = XMLProperty(xpath="./@type", default_cb=lambda s: "xen")
    hugepage = XMLProperty(xpath="./memoryBacking/hugepages", is_bool=True)
    bootloader = XMLProperty(xpath="./bootloader")
    description = XMLProperty(xpath="./description")
    emulator = XMLProperty(xpath="./devices/emulator")

    on_poweroff = XMLProperty(xpath="./on_poweroff",
                              default_cb=lambda s: "destroy")
    on_reboot = XMLProperty(xpath="./on_reboot")
    on_crash = XMLProperty(xpath="./on_crash")


    ###############################
    # Distro detection properties #
    ###############################

    def get_os_type(self):
        return self._os_type
    def set_os_type(self, val):
        val = val.lower()
        if val in self._OS_TYPES:
            if self._os_type != val:
                # Invalidate variant, since it may not apply to the new os type
                self._os_type = val
                self._os_variant = None
        else:
            raise ValueError(_("OS type '%s' does not exist in our "
                                "dictionary") % val)
    os_type = property(get_os_type, set_os_type)

    def get_os_variant(self):
        return self._os_variant
    def set_os_variant(self, val):
        val = val.lower()

        if self.os_type:
            if val in self._OS_TYPES[self.os_type]["variants"]:
                self._os_variant = val
            else:
                raise ValueError(_("OS variant '%(var)s' does not exist in "
                                   "our dictionary for OS type '%(ty)s'") %
                                   {'var' : val, 'ty' : self._os_type})
        else:
            found = False
            for ostype in self.list_os_types():
                if (val in self._OS_TYPES[ostype]["variants"] and
                    not self._OS_TYPES[ostype]["variants"][val].get("skip")):
                    logging.debug("Setting os type to '%s' for variant '%s'",
                                  ostype, val)
                    self.os_type = ostype
                    self._os_variant = val
                    found = True

            if not found:
                raise ValueError(_("Unknown OS variant '%s'" % val))
    os_variant = property(get_os_variant, set_os_variant)

    def get_os_distro(self):
        return self._lookup_osdict_key("distro")
    os_distro = property(get_os_distro)


    ########################################
    # Device Add/Remove Public API methods #
    ########################################

    def _dev_build_list(self, devtype, devlist=None):
        if devlist is None:
            devlist = self._devices

        newlist = []
        for i in devlist:
            if devtype == "all" or i.virtual_device_type == devtype:
                newlist.append(i)
        return newlist

    def add_device(self, dev):
        """
        Add the passed device to the guest's device list.

        @param dev: VirtualDevice instance to attach to guest
        @param set_defaults: Whether to set defaults for the device
        """
        self._track_device(dev)
        self._add_child(dev)
        self._recalculate_device_xpaths()

    def _track_device(self, dev):
        self._devices.append(dev)

    def get_devices(self, devtype):
        """
        Return a list of devices of type 'devtype' that will installed on
        the guest.

        @param devtype: Device type to search for (one of
                        VirtualDevice.virtual_device_types)
        """
        devlist = self._dev_build_list(devtype)
        return self._dev_build_list(devtype, devlist)

    def get_all_devices(self):
        """
        Return a list of all devices being installed with the guest
        """
        retlist = []
        for devtype in VirtualDevice.virtual_device_types:
            retlist.extend(self.get_devices(devtype))
        return retlist

    def remove_device(self, dev):
        """
        Remove the passed device from the guest's device list

        @param dev: VirtualDevice instance
        """
        found = False
        for devlist in [self._devices]:
            if found:
                break

            if dev in devlist:
                devlist.remove(dev)
                found = True
                break

        if not found:
            raise ValueError(_("Did not find device %s") % str(dev))

        self._remove_child(dev)
        self._recalculate_device_xpaths()


    ################################
    # Private xml building methods #
    ################################

    def _parsexml(self, xml, node):
        XMLBuilder._parsexml(self, xml, node)

        for node in self._xml_node.children or []:
            if node.name != "devices":
                continue

            devnodes = [
                x for x in node.children if
                (x.name in VirtualDevice.virtual_device_classes and
                 x.parent == node)
            ]
            for devnode in devnodes:
                objclass = VirtualDevice.virtual_device_classes[devnode.name]
                dev = objclass(self.conn, parsexmlnode=self._xml_node)
                self._track_device(dev)

        self._recalculate_device_xpaths()

    def _recalculate_device_xpaths(self):
        count = {}
        for dev in self.get_all_devices():
            devtype = dev.virtual_device_type
            if devtype not in count:
                count[devtype] = 1
            newpath = "./devices/%s[%d]" % (devtype, count[devtype])
            dev.set_root_xpath(newpath)
            count[devtype] += 1


    ############################
    # Install Helper functions #
    ############################

    def _prepare_install(self, meter, dry=False):
        for dev in self._install_devices:
            self.remove_device(dev)
        self._install_devices = []
        ignore = dry

        # Fetch install media, prepare installer devices
        self.installer.prepare(self, meter,
                               util.make_scratchdir(self.conn, self.type))

        # Initialize install device list
        for dev in self.installer.install_devices:
            self.add_device(dev)
            self._install_devices.append(dev)

    def _cleanup_install(self):
        self.installer.cleanup()

    def _create_devices(self, progresscb):
        """
        Ensure that devices are setup
        """
        for dev in self.get_all_devices():
            dev.setup(progresscb)

    all_devices = property(lambda s: s.get_all_devices())


    ##############
    # Public API #
    ##############

    def _prepare_get_xml(self):
        # We do a shallow copy of the device list here, and set the defaults.
        # This way, default changes aren't persistent, and we don't need
        # to worry about when to call set_defaults
        data = (self._devices, self.features, self.os)
        try:
            self._devices = [dev.copy() for dev in self._devices]
            self.features = self.features.copy()
            self.os = self.os.copy()
        except:
            self._finish_get_xml(data)
            raise
        return data

    def _finish_get_xml(self, data):
        self._devices, self.features, self.os = data

    def get_install_xml(self, *args, **kwargs):
        data = self._prepare_get_xml()
        try:
            return self._do_get_install_xml(*args, **kwargs)
        finally:
            self._finish_get_xml(data)

    def _do_get_install_xml(self, install=True, disk_boot=False):
        """
        Return the full Guest xml configuration.

        @param install: Whether we want the 'OS install' configuration or
                        the 'post-install' configuration. (Some Installers,
                        like the LiveCDInstaller may not have an 'install'
                        config.)
        @type install: C{bool}
        @param disk_boot: Whether we should boot off the harddisk, regardless
                          of our position in the install process (this is
                          used for 2 stage installs, where the second stage
                          boots off the disk. You probably don't need to touch
                          this.)
        @type disk_boot: C{bool}
        """
        osblob_install = install and not disk_boot
        if osblob_install and not self.installer.has_install_phase():
            return None

        self.installer.alter_bootconfig(self, osblob_install, self.os)
        self._set_transient_device_defaults(install)

        action = install and "destroy" or "restart"
        self.on_reboot = action
        self.on_crash = action

        self._set_defaults()

        self.bootloader = None
        if (not install and
            self.os.is_xenpv() and
            not self.os.kernel):
            self.bootloader = "/usr/bin/pygrub"
            self.os.clear()

        self._recalculate_device_xpaths()
        return self.get_xml_config()

    def get_continue_inst(self):
        """
        Return True if this guest requires a call to 'continue_install',
        which means the OS requires a 2 stage install (windows)
        """
        # If we are doing an 'import' or 'liveCD' install, there is
        # no true install process, so continue install has no meaning
        if not self.installer.has_install_phase():
            return False

        return self._lookup_osdict_key("continue")


    ##########################
    # Actual install methods #
    ##########################

    def start_install(self, meter=None,
                      dry=False, return_xml=False, noboot=False):
        """
        Begin the guest install (stage1).
        @param return_xml: Don't create the guest, just return generated XML
        """
        if self.domain is not None:
            raise RuntimeError(_("Domain has already been started!"))

        is_initial = True

        self._prepare_install(meter, dry)
        try:
            # Create devices if required (disk images, etc.)
            if not dry:
                self._create_devices(meter)

            start_xml, final_xml = self._build_xml(is_initial)
            if return_xml:
                return (start_xml, final_xml)
            if dry:
                return

            # Remove existing VM if requested
            self.check_vm_collision(self.conn, self.name,
                                    do_remove=self.replace)

            self.domain = self._create_guest(meter,
                                             start_xml, final_xml, is_initial,
                                             noboot)
            # Set domain autostart flag if requested
            self._flag_autostart()

            return self.domain
        finally:
            self._cleanup_install()

    def continue_install(self, meter=None,
                         dry=False, return_xml=False):
        """
        Continue with stage 2 of a guest install. Only required for
        guests which have the 'continue' flag set (accessed via
        get_continue_inst)
        """
        is_initial = False
        start_xml, final_xml = self._build_xml(is_initial)
        if return_xml:
            return (start_xml, final_xml)
        if dry:
            return

        return self._create_guest(meter,
                                  start_xml, final_xml, is_initial, False)

    def _build_meter(self, meter, is_initial):
        if is_initial:
            meter_label = _("Creating domain...")
        else:
            meter_label = _("Starting domain...")

        if meter is None:
            meter = progress.BaseMeter()
        meter.start(size=None, text=meter_label)

        return meter

    def _build_xml(self, is_initial):
        log_label = is_initial and "install" or "continue"
        disk_boot = not is_initial

        start_xml = self.get_install_xml(install=True, disk_boot=disk_boot)
        final_xml = self.get_install_xml(install=False)

        logging.debug("Generated %s XML: %s",
                      log_label,
                      (start_xml and ("\n" + start_xml) or "None required"))
        logging.debug("Generated boot XML: \n%s", final_xml)

        return start_xml, final_xml

    def _create_guest(self, meter,
                      start_xml, final_xml, is_initial, noboot):
        """
        Actually do the XML logging, guest defining/creating, console
        launching and waiting

        @param is_initial: If running initial guest creation, else we
                           are continuing the install
        @param noboot: Don't boot guest if no install phase
        """
        meter = self._build_meter(meter, is_initial)
        doboot = not noboot or self.installer.has_install_phase()

        if is_initial and doboot:
            dom = self.conn.createLinux(start_xml or final_xml, 0)
        else:
            dom = self.conn.defineXML(start_xml or final_xml)
            if doboot:
                dom.create()

        self.domain = dom
        meter.end(0)

        self.domain = self.conn.defineXML(final_xml)
        if is_initial:
            try:
                logging.debug("XML fetched from libvirt object:\n%s",
                              dom.XMLDesc(0))
            except Exception, e:
                logging.debug("Error fetching XML from libvirt object: %s", e)

        return self.domain


    def _flag_autostart(self):
        """
        Set the autostart flag for self.domain if the user requested it
        """
        if not self.autostart:
            return

        try:
            self.domain.setAutostart(True)
        except libvirt.libvirtError, e:
            if util.is_error_nosupport(e):
                logging.warn("Could not set autostart flag: libvirt "
                             "connection does not support autostart.")
            else:
                raise e


    ###################
    # Device defaults #
    ###################

    def add_default_input_device(self):
        if self.os.is_container():
            return
        self.add_device(virtinst.VirtualInputDevice(self.conn))

    def add_default_console_device(self):
        if self.os.is_xenpv():
            return
        dev = virtinst.VirtualConsoleDevice(self.conn)
        dev.type = dev.TYPE_PTY
        self.add_device(dev)

    def _set_transient_device_defaults(self, install):
        def do_remove_media(d):
            # Keep cdrom around, but with no media attached,
            # But only if we are a distro that doesn't have a multi
            # stage install (aka not Windows)
            return (d.virtual_device_type == "disk" and
                    d.device == VirtualDisk.DEVICE_CDROM
                    and d.transient
                    and not install and
                    not self.get_continue_inst())

        def do_skip_disk(d):
            # Skip transient labeled non-media disks
            return (d.virtual_device_type == "disk" and
                    d.device == VirtualDisk.DEVICE_DISK
                    and d.transient
                    and not install)

        for dev in self.get_all_devices():
            if do_skip_disk(dev):
                self.remove_device(dev)
                continue
            if do_remove_media(dev):
                dev.path = None

    def _set_defaults(self):
        self._set_osxml_defaults()
        self._set_clock_defaults()
        self._set_emulator_defaults()
        self._set_cpu_defaults()
        self._set_feature_defaults()

        for dev in self.get_all_devices():
            dev.set_defaults()
        self._add_implied_controllers()
        self._set_disk_defaults()
        self._set_net_defaults()
        self._set_input_defaults()
        self._set_sound_defaults()
        self._set_video_defaults()

    def _set_osxml_defaults(self):
        if self.os.is_container() and not self.os.init:
            for fs in self.get_devices("filesystem"):
                if fs.target == "/":
                    self.os.init = "/sbin/init"
                    break
            self.os.init = self.os.init or "/bin/sh"

        if not self.os.loader and self.os.is_hvm() and self.type == "xen":
            self.os.loader = "/usr/lib/xen/boot/hvmloader"
        if self.os.os_type == "xen" and self.type == "xen":
            # Use older libvirt 'linux' value for back compat
            self.os.os_type = "linux"
        if self.os.kernel or self.os.init:
            self.os.bootorder = []

        if (self.os.machine is None and
            self.conn.caps.host.arch == "ppc64"):
            self.os.machine = "pseries"

    def _set_clock_defaults(self):
        if not self.os.is_hvm():
            return

        if self.clock.offset is None:
            self.clock.offset = self._lookup_osdict_key("clock")

    def _set_emulator_defaults(self):
        if self.os.is_xenpv():
            self.emulator = None
            return

        if self.emulator:
            return

        if self.os.is_hvm() and self.type == "xen":
            if self.conn.caps.host.arch == "x86_64":
                self.emulator = "/usr/lib64/xen/bin/qemu-dm"
            else:
                self.emulator = "/usr/lib/xen/bin/qemu-dm"

    def _set_cpu_defaults(self):
        self.cpu.set_topology_defaults(self.vcpus)

    def _set_feature_defaults(self):
        if self.os.is_container():
            self.features.acpi = None
            self.features.apic = None
            self.features.pae = None
            return

        if not self.os.is_hvm():
            return

        if self.features["acpi"] == "default":
            self.features["acpi"] = self._lookup_osdict_key("acpi")
        if self.features["apic"] == "default":
            self.features["apic"] = self._lookup_osdict_key("apic")
        if self.features["pae"] == "default":
            self.features["pae"] = self.conn.caps.support_pae()

    def _add_implied_controllers(self):
        for dev in self.get_all_devices():
            # Add spapr-vio controller if needed
            if (dev.address.type == "spapr-vio" and
                dev.virtual_device_type == "disk" and
                not any([cont.address.type == "spapr-vio" for cont in
                        self.get_devices("controller")])):
                ctrl = virtinst.VirtualController(self.conn)
                ctrl.type = "scsi"
                ctrl.address.set_addrstr("spapr-vio")
                self.add_device(ctrl)

    def _set_disk_defaults(self):
        os_disk_bus  = self._lookup_device_param("disk", "bus")

        def set_disk_bus(d):
            if d.device == d.DEVICE_FLOPPY:
                d.bus = "fdc"
                return
            if self.os.is_xenpv():
                d.bus = "xen"
                return

            d.bus = "ide"
            if self.os.is_hvm():
                if (os_disk_bus and d.device == VirtualDisk.DEVICE_DISK):
                    d.bus = os_disk_bus
                elif (self.type == "kvm" and
                      self.os.machine == "pseries"):
                    d.bus = "scsi"

        used_targets = []
        for disk in self.get_devices("disk"):
            if not disk.bus:
                set_disk_bus(disk)

            # Default file backed PV guests to tap driver
            if (self.os.is_xenpv() and
                disk.type == VirtualDisk.TYPE_FILE and
                disk.driver_name is None and
                util.is_blktap_capable(self.conn)):
                disk.driver_name = VirtualDisk.DRIVER_TAP

            # Generate disk targets
            if disk.target:
                used_targets.append(disk.target)
            else:
                used_targets.append(disk.generate_target(used_targets))

    def _set_net_defaults(self):
        net_model = self._lookup_device_param("interface", "model")
        if not self.os.is_hvm():
            net_model = None

        for net in self.get_devices("interface"):
            if net_model and not net.model:
                net.model = net_model

    def _set_input_defaults(self):
        input_type = self._lookup_device_param("input", "type")
        input_bus = self._lookup_device_param("input", "bus")
        if self.os.is_xenpv():
            input_type = virtinst.VirtualInputDevice.TYPE_MOUSE
            input_bus = virtinst.VirtualInputDevice.BUS_XEN

        for inp in self.get_devices("input"):
            if (inp.type == inp.TYPE_DEFAULT and
                inp.bus  == inp.BUS_DEFAULT):
                inp.type = input_type
                inp.bus  = input_bus

    def _set_sound_defaults(self):
        sound_model = self._lookup_device_param("sound", "model")
        for sound in self.get_devices("sound"):
            if sound.model == sound.MODEL_DEFAULT:
                sound.model = sound_model

    def _set_video_defaults(self):
        # QXL device (only if we use spice) - safe even if guest is VGA only
        def has_spice():
            for gfx in self.get_devices("graphics"):
                if gfx.type == gfx.TYPE_SPICE:
                    return True
        if has_spice():
            video_model = "qxl"
        else:
            video_model = self._lookup_device_param("video", "model")

        for video in self.get_devices("video"):
            if video.model == video.MODEL_DEFAULT:
                video.model = video_model

        # Spice agent channel (only if we use spice)
        def has_spice_agent():
            for chn in self.get_devices("channel"):
                if chn.type == chn.TYPE_SPICEVMC:
                    return True

        if (has_spice() and
            not has_spice_agent() and
            self.conn.check_conn_support(
                                    self.conn.SUPPORT_CONN_HV_CHAR_SPICEVMC)):
            agentdev = virtinst.VirtualChannelDevice(self.conn)
            agentdev.type = agentdev.TYPE_SPICEVMC
            self.add_device(agentdev)


    ###################################
    # Guest Dictionary Helper methods #
    ###################################

    def _is_rhel6(self):
        emulator = self.emulator or ""

        return (self.type in ["qemu", "kvm"] and
                emulator.startswith("/usr/libexec/qemu"))

    def _lookup_osdict_key(self, key):
        """
        Using self.os_type and self.os_variant to find key in OSTYPES
        @returns: dict value, or None if os_type/variant wasn't set
        """
        return osdict.lookup_osdict_key(self.conn, self.type,
                                        self.os_type, self.os_variant,
                                        key)

    def _lookup_device_param(self, device_key, param):
        """
        Check the OS dictionary for the prefered device setting for passed
        device type and param (bus, model, etc.)
        """
        try:
            support.set_rhel6(self._is_rhel6())
            return osdict.lookup_device_param(self.conn, self.type,
                                              self.os_type, self.os_variant,
                                              device_key, param)
        finally:
            support.set_rhel6(False)